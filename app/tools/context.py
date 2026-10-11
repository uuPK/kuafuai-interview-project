"""Server-bound identity and task paths, never writable through tool arguments."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath

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

    def resolve_path(self, path: str, *, write: bool = False) -> Path:
        """Resolve task-relative paths; writes are restricted to scratch, including symlinks."""
        windows_path = PureWindowsPath(path)
        if not path or "\x00" in path or windows_path.drive or windows_path.root:
            raise PermissionError("path must be relative to the task directory")
        root = self.working_dir
        target = (root / path.replace("\\", "/")).resolve()
        boundary = root / "scratch" if write else root
        if not target.is_relative_to(boundary) or target == boundary:
            if not write and target == root:
                return target
            raise PermissionError("path is outside the permitted task directory")
        return target


def bind_context(runs: RunStore, data_dir: Path, run_id: str, user_id: str) -> ToolContext:
    run = runs.get_run(run_id, user_id)
    data_root = data_dir.resolve()
    base = data_root / "runs"
    root = (base / run.id).resolve()
    if not base.resolve().is_relative_to(data_root) or not root.is_relative_to(base):
        raise PermissionError("task directory resolves outside the configured root")
    return ToolContext(
        run_id=run.id,
        user_id=run.user_id,
        recipient=run.subscription.email,
        working_dir=root,
        window_start=run.window_start,
        window_end=run.window_end,
        deadline_at=run.deadline_at,
    )
