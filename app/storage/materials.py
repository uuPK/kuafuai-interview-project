"""Run-bound read versions, opaque search cursors and durable scratch write intents."""

import json
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from pydantic import Field, model_validator

from app.agent.research import selection_notes
from app.harness.execution import file_publication
from app.harness.redaction import Redactor
from app.storage.models import StoredModel
from app.storage.snapshots import SnapshotFiles, _safe_path, digest
from app.tools.models import ToolFailure, ToolResult


class WriteJob(StoredModel):
    run_id: str
    tool_call_id: str
    path: str
    original_hash: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    target_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(repr=False)
    result: ToolResult

    @model_validator(mode="after")
    def validate_intent(self):
        expected = ToolResult.success(
            {
                "path": self.path,
                "content_hash": self.target_hash,
                "char_count": len(self.content),
                "unchanged": self.original_hash == self.target_hash,
            }
        )
        if digest(self.content.encode("utf-8")) != self.target_hash or self.result != expected:
            raise ValueError("write intent content/hash/result mismatch")
        return self


class MaterialStore:
    def __init__(self, sources, settings):
        self.sources, self.settings = sources, settings
        self.redactor = Redactor(settings)
        self.runs, self.database = sources.runs, sources.database
        self.files = SnapshotFiles(settings.data_dir, settings.max_material_file_bytes)

    def read_hash(self, context, path):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            row = connection.execute(
                "SELECT content_hash FROM material_reads WHERE run_id=? AND path=?",
                (context.run_id, path),
            ).fetchone()
        return row[0] if row else None

    def on_read(self, connection, call):
        if call.result.ok:
            data = call.result.data
            connection.execute(
                "INSERT INTO material_reads VALUES(?,?,?) ON CONFLICT(run_id,path) "
                "DO UPDATE SET content_hash=excluded.content_hash",
                (call.run_id, data["path"], data["content_hash"]),
            )

    def cursor(self, context, token, directory, keyword_hash, manifest):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            row = connection.execute(
                "SELECT * FROM material_cursors WHERE id=? AND run_id=?", (token, context.run_id)
            ).fetchone()
        if row is None or row["directory"] != directory or row["keyword_hash"] != keyword_hash:
            raise ToolFailure("FILE_CURSOR_INVALID", "搜索游标无效或不属于本任务及查询。")
        if json.loads(row["manifest_json"]) != manifest:
            raise ToolFailure("FILE_VERSION_CHANGED", "搜索材料已变化，请去掉 cursor 重新定位。")
        return row["file_position"], row["char_position"]

    def save_cursor(self, context, token, directory, keyword_hash, manifest, file_pos, char_pos):
        with self.database.transaction() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            connection.execute(
                "INSERT INTO material_cursors VALUES(?,?,?,?,?,?,?)",
                (
                    token,
                    context.run_id,
                    directory,
                    keyword_hash,
                    json.dumps(manifest, ensure_ascii=False),
                    file_pos,
                    char_pos,
                ),
            )

    def job_path(self, context):
        if not context.tool_call_id or not re.fullmatch(r"[a-f0-9-]{36}", context.run_id):
            raise PermissionError("write intent requires a server-bound call")
        return _safe_path(
            self.settings.data_dir,
            f"file_jobs/{context.run_id}/{digest(context.tool_call_id.encode())}.json",
        )

    def write(self, context, path, target: Path, content, current_hash):
        context.assert_current()
        raw = content.encode("utf-8")
        desired = digest(raw)
        intent_path = self.job_path(context)
        if intent_path.exists():
            try:
                job = WriteJob.model_validate_json(self.files._read(intent_path))
            except (ValueError, ToolFailure):
                raise ToolFailure("FILE_WRITE_STATE_PROBLEM", "文件写入登记材料损坏。") from None
            if (job.run_id, job.tool_call_id, job.path, job.target_hash, job.content) != (
                context.run_id,
                context.tool_call_id,
                path,
                desired,
                content,
            ):
                raise ToolFailure("FILE_WRITE_STATE_PROBLEM", "已记录写入意图与本次调用不一致。")
            if current_hash == desired:
                return job.result
            if current_hash != job.original_hash:
                raise ToolFailure(
                    "FILE_WRITE_CONFLICT", "文件已被其他修改改变，不能恢复覆盖；请重新读取。"
                )
        else:
            result = ToolResult.success(
                {
                    "path": path,
                    "content_hash": desired,
                    "char_count": len(content),
                    "unchanged": current_hash == desired,
                }
            )
            if len(result.model_dump_json()) > self.settings.max_tool_result_chars:
                raise ToolFailure("RESULT_TOO_LARGE", "文件写入结果超过边界，请缩短路径。")
            job = WriteJob(
                run_id=context.run_id,
                tool_call_id=context.tool_call_id,
                path=path,
                original_hash=current_hash,
                target_hash=desired,
                content=content,
                result=result,
            )
            try:
                self.files.publish(intent_path, job.model_dump_json().encode("utf-8"))
            except ToolFailure:
                raise ToolFailure(
                    "FILE_WRITE_STATE_PROBLEM", "文件写入登记材料无法一致保存。"
                ) from None
        if current_hash == desired:
            return job.result
        target.parent.mkdir(parents=True, exist_ok=True)
        context.resolve_path(path, write=True)
        before = None
        if target.exists():
            with target.open("rb") as file:
                previous = file.read(self.settings.max_material_file_bytes + 1)
            if len(previous) > self.settings.max_material_file_bytes:
                raise ToolFailure("FILE_TOO_LARGE", "写入前文件已超出大小限制。")
            before = digest(previous)
        if before != job.original_hash:
            raise ToolFailure("FILE_WRITE_CONFLICT", "文件在写入前已变化，请重新读取。")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".pending-", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            with file_publication():
                latest = digest(self.files._read(target)) if target.exists() else None
                if latest != job.original_hash:
                    raise ToolFailure("FILE_WRITE_CONFLICT", "文件在发布前已变化，请重新读取。")
                os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return job.result

    def on_written(self, connection, call):
        if call.result.ok:
            self.runs._append_event(
                connection,
                call.run_id,
                "material_written",
                self.runs.now(),
                {"tool_call_id": call.tool_call_id, **call.result.data},
            )
            if call.result.data["path"] == "scratch/brief.json":
                try:
                    content = json.loads(call.arguments)["content"]
                except (ValueError, KeyError, TypeError):
                    content = ""
                notes = selection_notes(self.redactor.clean(content))
                if notes:
                    self.runs._append_event(
                        connection,
                        call.run_id,
                        "research_selections",
                        self.runs.now(),
                        {
                            "tool_call_id": call.tool_call_id,
                            "items": notes,
                            "basis": "model_reported",
                        },
                    )

    @staticmethod
    def new_cursor():
        return uuid4().hex
