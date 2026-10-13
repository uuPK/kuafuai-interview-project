"""Register source metadata, indices and tool feedback in the same database transaction."""

from pydantic import ValidationError

from app.config import Settings
from app.providers.article import TextIndex
from app.storage.candidates import CandidateStore
from app.storage.models import timestamp
from app.storage.snapshots import SnapshotFiles, SourceMetadata
from app.tools.context import bind_context
from app.tools.models import ToolFailure


class SourceStore:
    def __init__(self, candidates: CandidateStore, settings: Settings):
        self.candidates, self.settings = candidates, settings
        self.runs, self.calls = candidates.runs, candidates.calls
        self.database = candidates.database
        self.files = SnapshotFiles(settings.data_dir, settings.max_download_bytes)

    def get_source(self, run_id, user_id, source_id):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            row = connection.execute(
                "SELECT * FROM sources WHERE run_id=? AND id=?", (run_id, source_id)
            ).fetchone()
            if row is None:
                raise LookupError("source not found in current run")
            try:
                metadata = SourceMetadata.model_validate_json(row["metadata_json"])
                index = TextIndex.model_validate_json(row["index_json"])
            except ValidationError:
                raise ToolFailure(
                    "SOURCE_STATE_PROBLEM", "数据库中的来源元数据或索引不完整。"
                ) from None
        context = bind_context(self.runs, self.settings.data_dir, run_id, user_id)
        self.files.verify(context, metadata, index)
        return metadata, index

    def duplicate_hints(self, run_id, user_id, url, content_hash):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            rows = connection.execute(
                "SELECT id, url, content_hash FROM sources "
                "WHERE run_id=? AND (url=? OR content_hash=?) ORDER BY rowid",
                (run_id, url, content_hash),
            ).fetchall()
        return {
            "same_url": [row["id"] for row in rows if row["url"] == url],
            "same_body": [row["id"] for row in rows if row["content_hash"] == content_hash],
        }

    def on_completed(self, connection, call):
        if not call.result.ok:
            return
        run = self.runs._owned_row(connection, call.run_id, self._user(connection, call.run_id))
        context = bind_context(
            self.runs,
            self.settings.data_dir,
            call.run_id,
            run["user_id"],
            tool_call_id=call.tool_call_id,
        )
        job = self.files.load_job(context)
        if job is None or job.result != call.result:
            raise ToolFailure("SOURCE_STATE_PROBLEM", "来源登记材料与工具结果不一致。")
        metadata = job.metadata
        candidate = connection.execute(
            "SELECT run_id FROM candidates WHERE id=?", (metadata.candidate_id,)
        ).fetchone()
        if candidate is None or candidate["run_id"] != call.run_id:
            raise PermissionError("source candidate does not belong to current run")
        existing = connection.execute(
            "SELECT * FROM sources WHERE id=?", (metadata.source_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["metadata_json"] != metadata.model_dump_json()
                or existing["index_json"] != job.index.model_dump_json()
            ):
                raise ToolFailure("SOURCE_STATE_PROBLEM", "已登记的来源快照不能覆盖。")
        else:
            connection.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                (
                    metadata.source_id,
                    call.run_id,
                    metadata.url,
                    metadata.content_hash,
                    metadata.model_dump_json(),
                    job.index.model_dump_json(),
                ),
            )
        connection.execute(
            "INSERT INTO source_calls VALUES (?, ?, ?, ?)",
            (
                call.run_id,
                call.tool_call_id,
                metadata.candidate_id,
                metadata.source_id,
            ),
        )
        self.runs._append_event(
            connection,
            call.run_id,
            "source_saved",
            self.runs.now(),
            {
                "tool_call_id": call.tool_call_id,
                "source_id": metadata.source_id,
                "candidate_id": metadata.candidate_id,
                "content_hash": metadata.content_hash,
                "char_count": metadata.char_count,
                "publication_status": metadata.publication.status,
                "fetched_at": timestamp(metadata.fetched_at),
                "duplicates": call.result.data["duplicates"],
            },
        )

    @staticmethod
    def _user(connection, run_id):
        return connection.execute("SELECT user_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
