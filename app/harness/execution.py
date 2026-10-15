"""Worker-local claim capability; fence storage and file publication after takeover."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class LeaseLostError(RuntimeError):
    """The caller no longer owns this run; do not turn this into model feedback."""


@dataclass(frozen=True)
class ExecutionClaim:
    database: object
    run_id: str
    token: str
    clock: object


CURRENT_CLAIM = ContextVar("news_execution_claim", default=None)


@contextmanager
def execution_scope(claim):
    marker = CURRENT_CLAIM.set(claim)
    try:
        yield
    finally:
        CURRENT_CLAIM.reset(marker)


def assert_execution(connection, run_id=None, *, writing=False):
    claim = CURRENT_CLAIM.get()
    if claim is None and not writing:
        return
    if claim is not None:
        location = connection.execute("PRAGMA database_list").fetchone()[2]
        if Path(location).resolve() != claim.database.path or (run_id and run_id != claim.run_id):
            raise LeaseLostError("claim does not own this database/run")
        run_id = claim.run_id
    # Older migration fixtures intentionally have no lease table.
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_leases'"
    ).fetchone():
        if claim is not None:
            raise LeaseLostError("claim storage is missing")
        return
    row = connection.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
    if claim is None:
        if row is not None:
            raise LeaseLostError("a worker claim is required to update this run")
        return
    from app.storage.models import timestamp

    if row is None or row["token"] != claim.token or row["expires_at"] <= timestamp(claim.clock()):
        raise LeaseLostError("worker claim expired or was replaced")


def check_current_execution():
    claim = CURRENT_CLAIM.get()
    if claim is not None:
        with claim.database.connection() as connection:
            assert_execution(connection)


def file_publication():
    """Serialize the final local publication with lease takeover, never with HTTP work."""
    claim = CURRENT_CLAIM.get()
    return claim.database.transaction() if claim is not None else nullcontext()
