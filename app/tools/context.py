"""Server-bound identity and task paths, never writable through tool arguments."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath

from app.harness.execution import (
    CURRENT_CLAIM,
    LeaseLostError,
    assert_execution,
    check_current_execution,
)
from app.storage.runs import RunStore


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    user_id: str
    recipient: str = field(repr=False)
    working_dir: Path = field(repr=False)
    window_start: datetime
    window_end: datetime
    deadline_at: datetime | None
    tool_call_id: str | None = None
    execution_token: str | None = field(default=None, repr=False)

    def assert_current(self):
        check_current_execution()
        current = CURRENT_CLAIM.get()
        if current is not None and current.run_id != self.run_id:
            raise LeaseLostError("tool context belongs to another run")
        if self.execution_token is not None:
            if current is None or current.token != self.execution_token:
                raise LeaseLostError("tool context belongs to a previous execution")

    def resolve_path(self, path: str, *, write: bool = False) -> Path:
        """Resolve task-relative paths; writes are restricted to scratch, including symlinks."""
        self.assert_current()
        windows_path = PureWindowsPath(path)
        if not path or "\x00" in path or windows_path.drive or windows_path.root:
            raise PermissionError("path must be relative to the task directory")
        root = self.working_dir
        if root.resolve() != root:
            raise PermissionError("task root changed")
        current = root
        for part in Path(path.replace("\\", "/")).parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise PermissionError("task paths cannot use links")
        target = (root / path.replace("\\", "/")).resolve()
        boundary = root / "scratch" if write else root
        if not target.is_relative_to(boundary) or target == boundary:
            if not write and target == root:
                return target
            raise PermissionError("path is outside the permitted task directory")
        return target


def bind_context(
    runs: RunStore, data_dir: Path, run_id: str, user_id: str, *, tool_call_id: str | None = None
) -> ToolContext:
    run = runs.get_run(run_id, user_id)
    with runs.database.connection() as connection:
        assert_execution(connection, run_id, writing=True)
    data_root = data_dir.resolve()
    base = data_root / "runs"
    expected = base / run.id
    root = expected.resolve()
    if base.resolve() != base or root != expected or not root.is_relative_to(base):
        raise PermissionError("task directory resolves outside the configured root")
    return ToolContext(
        run_id=run.id,
        user_id=run.user_id,
        recipient=run.subscription.email,
        working_dir=root,
        window_start=run.window_start,
        window_end=run.window_end,
        deadline_at=run.deadline_at,
        tool_call_id=tool_call_id,
        execution_token=CURRENT_CLAIM.get().token if CURRENT_CLAIM.get() is not None else None,
    )
