"""Owner-checked cursor pagination over safe task events; no raw protocol query."""

import json

from pydantic import Field

from app.harness.trace import LABELS
from app.storage.models import EventPage, RunEvent, RunStatus, StoredModel
from app.tools.models import ToolFailure


class TimelineQuery(EventPage):
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    after_id: int = Field(default=0, ge=0, strict=True)


class TimelineEvent(RunEvent):
    summary: str
    reason_basis: str


class TimelinePage(StoredModel):
    run_id: str
    status: RunStatus
    stop_reason: str | None
    events: tuple[TimelineEvent, ...]
    next_after_id: int
    has_more: bool


class TimelineStore:
    def __init__(self, runs):
        self.runs, self.database = runs, runs.database

    def page(self, run_id, user_id, *, after_id=0, limit=50):
        query = TimelineQuery(after_id=after_id, limit=limit)
        with self.database.connection() as connection:
            run = self.runs._owned_row(connection, run_id, user_id)
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                (run_id, query.after_id, query.limit + 1),
            ).fetchall()
        events = []
        try:
            for row in rows[: query.limit]:
                data = self.runs.projection.data(json.loads(row["data_json"]))
                kind = self.runs.projection.text(row["kind"], 100)
                events.append(
                    TimelineEvent(
                        id=row["id"],
                        run_id=run_id,
                        kind=kind,
                        occurred_at=row["occurred_at"],
                        data=data,
                        summary=LABELS.get(kind, "任务事件"),
                        reason_basis="model_reported"
                        if data.get("basis") == "model_reported"
                        else "saved_feedback"
                        if data.get("issues") or data.get("error_code")
                        else "missing",
                    )
                )
        except (ValueError, TypeError, RecursionError):
            raise ToolFailure("TIMELINE_STATE_PROBLEM", "任务事件记录损坏，无法展示。") from None
        return TimelinePage(
            run_id=run_id,
            status=run["status"],
            stop_reason=run["stop_reason"],
            events=tuple(events),
            next_after_id=events[-1].id if events else query.after_id,
            has_more=len(rows) > query.limit,
        )
