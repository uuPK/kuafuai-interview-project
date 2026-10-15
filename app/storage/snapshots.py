"""Immutable UTF-8 source files and a durable manifest for incomplete registration."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import AwareDatetime, Field

from app.harness.execution import check_current_execution, file_publication
from app.providers.article import Publication, TextIndex
from app.storage.models import StoredModel
from app.tools.context import ToolContext
from app.tools.models import ToolFailure, ToolResult


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SourceMetadata(StoredModel):
    source_id: str = Field(pattern=r"^src-[a-f0-9]{32}$")
    run_id: str
    candidate_id: str
    url: str
    title: str
    language: str | None
    fetched_at: AwareDatetime
    char_count: int = Field(gt=0)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    index_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_path: str
    index_path: str
    publication: Publication
    extraction_version: str
    redirects: tuple[str, ...]


class SourceJob(StoredModel):
    tool_call_id: str
    metadata: SourceMetadata
    index: TextIndex
    result: ToolResult


def _safe_path(root: Path, relative: str) -> Path:
    root = root.absolute()
    if root.resolve() != root:
        raise PermissionError("snapshot root changed through a symlink")
    target = root / relative
    if not target.resolve().is_relative_to(root):
        raise PermissionError("snapshot path escaped its root")
    current = root
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            raise PermissionError("invalid snapshot path")
        current = current / part
        if current.is_symlink():
            raise PermissionError("snapshot paths cannot use symlinks")
    return target


class SnapshotFiles:
    def __init__(self, data_dir: Path, max_bytes: int):
        self.data_dir, self.max_bytes = data_dir, max_bytes

    def job_path(self, context: ToolContext) -> Path:
        if not context.tool_call_id or not re.fullmatch(r"[a-f0-9-]{36}", context.run_id):
            raise PermissionError("snapshot job requires a server-bound run and call")
        name = digest(context.tool_call_id.encode("utf-8"))
        return _safe_path(self.data_dir, f"source_jobs/{context.run_id}/{name}.json")

    @staticmethod
    def _problem():
        raise ToolFailure(
            "SOURCE_STATE_PROBLEM", "正文快照、索引或登记材料缺失/不一致，请检查任务材料或换来源。"
        )

    def _read(self, path: Path) -> bytes:
        try:
            if path.stat().st_size > self.max_bytes * 8 + 100_000:
                self._problem()
            return path.read_bytes()
        except (OSError, UnicodeError):
            self._problem()

    def publish(self, path: Path, content: bytes):
        check_current_execution()
        if len(content) > self.max_bytes * 8 + 100_000:
            raise ToolFailure("FETCH_EXTRACTED_TOO_LARGE", "提取后的材料或位置索引超过存储边界。")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self._read(path) != content:
                self._problem()
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".pending-", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            # Atomic publication without replacing an existing immutable file.
            try:
                with file_publication():
                    os.link(temporary, path)
            except FileExistsError:
                if self._read(path) != content:
                    self._problem()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load_job(self, context: ToolContext, *, repair_index=False) -> SourceJob | None:
        context.assert_current()
        path = self.job_path(context)
        if not path.exists():
            return None
        try:
            job = SourceJob.model_validate_json(self._read(path))
            if job.metadata.run_id != context.run_id or job.tool_call_id != context.tool_call_id:
                self._problem()
            if repair_index:
                self.restore_index(context, job.metadata, job.index)
            self.verify(context, job.metadata, job.index)
            return job
        except (ValueError, UnicodeError):
            self._problem()

    def restore_index(self, context, metadata, index):
        context.assert_current()
        expected = f"sources/{metadata.source_id}/"
        encoded = index.model_dump_json().encode("utf-8")
        if (
            metadata.run_id != context.run_id
            or metadata.file_path != expected + "body.txt"
            or metadata.index_path != expected + "index.json"
            or digest(encoded) != metadata.index_hash
        ):
            self._problem()
        body = self._read(_safe_path(context.working_dir, metadata.file_path))
        if digest(body) != metadata.content_hash:
            self._problem()
        path = _safe_path(context.working_dir, metadata.index_path)
        if not path.exists():
            self.publish(path, encoded)
        self.verify(context, metadata, index)

    def save_job(self, context: ToolContext, job: SourceJob, body: str):
        context.assert_current()
        # Write intent first: an incomplete file set never silently becomes a fresh snapshot.
        self.publish(self.job_path(context), job.model_dump_json().encode("utf-8"))
        self.publish(_safe_path(context.working_dir, job.metadata.file_path), body.encode("utf-8"))
        self.publish(
            _safe_path(context.working_dir, job.metadata.index_path),
            job.index.model_dump_json().encode("utf-8"),
        )
        self.verify(context, job.metadata, job.index)

    def verify(self, context, metadata, index):
        if metadata.run_id != context.run_id:
            raise PermissionError("source snapshot belongs to another run")
        expected = f"sources/{metadata.source_id}/"
        if (
            metadata.file_path != expected + "body.txt"
            or metadata.index_path != expected + "index.json"
        ):
            self._problem()
        body_bytes = self._read(_safe_path(context.working_dir, metadata.file_path))
        index_bytes = self._read(_safe_path(context.working_dir, metadata.index_path))
        if (
            digest(body_bytes) != metadata.content_hash
            or digest(index_bytes) != metadata.index_hash
        ):
            self._problem()
        try:
            body = body_bytes.decode("utf-8")
        except UnicodeError:
            self._problem()
        if (
            len(body) != metadata.char_count
            or "\r" in body
            or index_bytes != index.model_dump_json().encode("utf-8")
        ):
            self._problem()
        for block in (*index.blocks, *index.sections):
            if not 0 <= block.start_offset < block.end_offset <= len(body):
                self._problem()
        return body
