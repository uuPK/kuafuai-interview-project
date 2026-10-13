"""Run-owned progress and a trusted ledger of facts from saved, accepted briefs."""

import json
from datetime import timedelta

from pydantic import ValidationError

from app.agent.research import HistoryEvent, ResearchProgress
from app.harness.redaction import Redactor
from app.storage.models import timestamp
from app.storage.snapshots import SourceMetadata
from app.tools.models import ToolFailure


class ResearchStore:
    def __init__(self, calls, settings):
        self.calls, self.runs, self.database = calls, calls.runs, calls.database
        self.redactor = Redactor(settings)

    def progress(self, run_id, user_id):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            row = connection.execute(
                "SELECT progress_json FROM run_progress WHERE run_id=?", (run_id,)
            ).fetchone()
        try:
            return (
                ResearchProgress.model_validate_json(row[0])
                if row
                else ResearchProgress(collected=(), missing=(), issues=())
            )
        except ValidationError:
            raise ToolFailure("PROGRESS_STATE_PROBLEM", "已保存的任务进度损坏。") from None

    def on_progress(self, connection, call):
        if not call.result.ok:
            return
        row = connection.execute("SELECT * FROM runs WHERE id=?", (call.run_id,)).fetchone()
        if row["status"] != "running":
            raise PermissionError("progress requires a running run")
        progress = ResearchProgress.model_validate_json(
            json.dumps(call.result.data["progress"], ensure_ascii=False)
        )
        at = self.runs.now()
        connection.execute(
            """INSERT INTO run_progress VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET tool_call_id=excluded.tool_call_id,
                progress_json=excluded.progress_json, updated_at=excluded.updated_at""",
            (call.run_id, call.tool_call_id, progress.model_dump_json(), timestamp(at)),
        )
        self.runs._append_event(
            connection,
            call.run_id,
            "progress_updated",
            at,
            {
                "tool_call_id": call.tool_call_id,
                **progress.model_dump(mode="json"),
                "basis": "model_reported",
            },
        )

    def record_delivery(self, connection, run_id, user_id, brief_id, events, *, delivered_at=None):
        """Trusted publisher API; integrate with checked brief persistence in step 3.1."""
        if not connection.in_transaction:
            raise ValueError("delivery requires an active publisher transaction")
        run = self.runs._owned_row(connection, run_id, user_id)
        if run["status"] != "completed":
            raise PermissionError("history requires an accepted completed brief")
        if (
            not isinstance(brief_id, str)
            or not 1 <= len(brief_id) <= 128
            or not 1 <= len(events) <= 5
        ):
            raise ValueError("invalid delivery identity or event count")
        events = tuple(HistoryEvent.model_validate(event) for event in events)
        serialized = [
            HistoryEvent.model_validate_json(
                json.dumps(self.redactor.clean(event.model_dump(mode="json")), ensure_ascii=False)
            ).model_dump_json()
            for event in events
        ]
        existing = connection.execute(
            "SELECT * FROM delivered_history WHERE run_id=? OR brief_id=? ORDER BY position",
            (run_id, brief_id),
        ).fetchall()
        if existing:
            if len(existing) != len(events) or any(
                row["run_id"] != run_id
                or row["brief_id"] != brief_id
                or row["position"] != index
                or row["event_json"] != serialized[index]
                for index, row in enumerate(existing)
            ):
                raise ValueError("delivered history is immutable")
            return
        at = self.runs.now(delivered_at)
        if timestamp(at) < run["updated_at"] or at > self.runs.now():
            raise ValueError("delivery time must follow completion and not be future")
        for event in events:
            for citation in event.citations:
                source = connection.execute(
                    "SELECT metadata_json FROM sources WHERE id=? AND run_id=?",
                    (citation.source_id, run_id),
                ).fetchone()
                if source is None:
                    raise PermissionError("history source must belong to this delivered run")
                metadata = SourceMetadata.model_validate_json(source[0])
                if metadata.run_id != run_id or metadata.source_id != citation.source_id:
                    raise ValueError("history source identity mismatch")
                if citation.end_offset > metadata.char_count:
                    raise ValueError("history citation exceeds its source")
        for position in range(len(events)):
            connection.execute(
                "INSERT INTO delivered_history VALUES (?, ?, ?, ?, ?)",
                (brief_id, position, run_id, timestamp(at), serialized[position]),
            )
        self.runs._append_event(
            connection,
            run_id,
            "history_recorded",
            at,
            {
                "brief_id": brief_id,
                "event_count": len(events),
            },
        )

    def history(self, context, days, limit):
        lower, upper = (
            timestamp(context.window_end - timedelta(days=days)),
            timestamp(context.window_end),
        )
        with self.database.connection() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            condition = """FROM delivered_history h JOIN runs r ON r.id=h.run_id
                WHERE r.user_id=? AND r.status='completed' AND h.run_id!=?
                    AND h.delivered_at>=? AND h.delivered_at<=?"""
            args = (context.user_id, context.run_id, lower, upper)
            total = connection.execute("SELECT COUNT(*) " + condition, args).fetchone()[0]
            rows = connection.execute(
                "SELECT h.* "
                + condition
                + " ORDER BY h.delivered_at DESC, h.brief_id, h.position LIMIT ?",
                (*args, limit),
            ).fetchall()
            items = []
            try:
                for row in rows:
                    event = HistoryEvent.model_validate_json(row["event_json"])
                    citations = []
                    for ref in event.citations:
                        source = connection.execute(
                            "SELECT metadata_json FROM sources WHERE run_id=? AND id=?",
                            (row["run_id"], ref.source_id),
                        ).fetchone()
                        if source is None:
                            raise ValueError("history source missing")
                        metadata = SourceMetadata.model_validate_json(source[0])
                        if metadata.run_id != row["run_id"] or metadata.source_id != ref.source_id:
                            raise ValueError("history source identity mismatch")
                        if ref.end_offset > metadata.char_count:
                            raise ValueError("history citation exceeds its source")
                        citations.append(
                            {
                                **ref.model_dump(),
                                "url": metadata.url,
                                "content_hash": metadata.content_hash,
                            }
                        )
                    items.append(
                        {
                            **event.model_dump(mode="json"),
                            "citations": citations,
                            "brief_id": row["brief_id"],
                            "delivered_at": row["delivered_at"],
                        }
                    )
            except (ValueError, TypeError):
                raise ToolFailure(
                    "HISTORY_STATE_PROBLEM", "历史记录或来源关系损坏，请核查存储。"
                ) from None
        return self.redactor.clean(items), total
