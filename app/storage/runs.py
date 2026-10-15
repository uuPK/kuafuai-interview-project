"""Run persistence with user ownership, fixed windows, and atomic event updates."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.config import Settings
from app.harness.execution import assert_execution
from app.harness.trace import EventProjection
from app.storage.database import Database
from app.storage.models import (
    BudgetCharge,
    BudgetLimits,
    BudgetUsage,
    EventPage,
    RunEvent,
    RunRecord,
    RunRequest,
    RunStatus,
    StateChange,
    SubscriptionSnapshot,
    as_utc,
    timestamp,
)

TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_RETRY,
        RunStatus.COMPLETED,
        RunStatus.NO_NEWS,
        RunStatus.FAILED,
    },
    RunStatus.WAITING_RETRY: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.NO_NEWS: set(),
    RunStatus.FAILED: set(),
}


class RunNotFoundError(LookupError):
    """Unknown run and another user's run intentionally have the same error."""


class InvalidRunStateError(ValueError):
    """An operation is invalid for the run's persisted state."""


class BudgetExceededError(RuntimeError):
    def __init__(self, dimension: str) -> None:
        self.dimension = dimension
        super().__init__(f"run budget exhausted: {dimension}")


def _record(row: sqlite3.Row) -> RunRecord:
    values = dict(row)
    values["subscription"] = SubscriptionSnapshot.model_validate_json(
        values.pop("subscription_json")
    )
    values["budget_limits"] = BudgetLimits.model_validate_json(values.pop("budget_limits_json"))
    values["budget_usage"] = BudgetUsage.model_validate_json(values.pop("budget_usage_json"))
    return RunRecord.model_validate(values)


class RunStore:
    def __init__(
        self, database: Database, settings: Settings, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.database = database
        self.budget_limits = BudgetLimits.from_settings(settings)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.projection = EventProjection(settings)
        self.database.event_sink = self.projection.emit

    def now(self, value: datetime | None = None) -> datetime:
        return as_utc(value if value is not None else self.clock())

    @staticmethod
    def _owned_row(connection: sqlite3.Connection, run_id: str, user_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)
        ).fetchone()
        if row is None:
            raise RunNotFoundError("run not found")
        assert_execution(connection, run_id, writing=connection.in_transaction)
        return row

    def _append_event(
        self, connection: sqlite3.Connection, run_id: str, kind: str, at: datetime, data: dict
    ) -> None:
        """Keep event metadata separate from controlled protocol, with credentials removed."""
        assert_execution(connection, run_id, writing=True)
        data = self.projection.redactor.clean(json.loads(json.dumps(data, ensure_ascii=False)))
        connection.execute(
            "INSERT INTO run_events (run_id, kind, occurred_at, data_json) VALUES (?, ?, ?, ?)",
            (run_id, kind, timestamp(at), json.dumps(data, ensure_ascii=False)),
        )

    def create_run(self, request: RunRequest, *, now: datetime | None = None) -> RunRecord:
        request = RunRequest.model_validate(request)
        created = self.now(now)
        window_end = request.scheduled_for if request.scheduled_for is not None else created
        run_id = str(uuid4())
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO runs (
                    id, user_id, subscription_id, mode, status, subscription_json,
                    budget_limits_json, budget_usage_json, created_at, updated_at,
                    scheduled_for, window_start, window_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    request.user_id,
                    request.subscription_id,
                    request.mode,
                    RunStatus.QUEUED,
                    request.subscription.model_dump_json(),
                    self.budget_limits.model_dump_json(),
                    BudgetUsage().model_dump_json(),
                    timestamp(created),
                    timestamp(created),
                    timestamp(request.scheduled_for) if request.scheduled_for is not None else None,
                    timestamp(window_end - timedelta(hours=24)),
                    timestamp(window_end),
                ),
            )
            self._append_event(connection, run_id, "run_created", created, {"mode": request.mode})
            return _record(self._owned_row(connection, run_id, request.user_id))

    def get_run(self, run_id: str, user_id: str) -> RunRecord:
        with self.database.connection() as connection:
            return _record(self._owned_row(connection, run_id, user_id))

    @staticmethod
    def _check_clock(run: RunRecord, at: datetime) -> None:
        if at < run.updated_at:
            raise ValueError("operation timestamp precedes the last run update")

    def transition(
        self, run_id: str, user_id: str, change: StateChange, *, now: datetime | None = None
    ) -> RunRecord:
        change = StateChange.model_validate(change)
        with self.database.transaction() as connection:
            run = _record(self._owned_row(connection, run_id, user_id))
            return self._transition(connection, run, change, self.now(now))

    def _transition(
        self, connection: sqlite3.Connection, run: RunRecord, change: StateChange, at: datetime
    ) -> RunRecord:
        """Coordinate a terminal state with its submission result and tool feedback."""
        self._check_clock(run, at)
        if change.status not in TRANSITIONS[run.status]:
            raise InvalidRunStateError(f"cannot change {run.status} to {change.status}")
        if (
            run.deadline_at is not None
            and change.status in {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.NO_NEWS}
            and at >= run.deadline_at
        ):
            raise BudgetExceededError("deadline")
        started, deadline = run.started_at, run.deadline_at
        if change.status == RunStatus.RUNNING:
            if run.retry_at is not None and at < run.retry_at:
                raise InvalidRunStateError("retry is not due yet")
            if started is None:
                started = at
                deadline = at + timedelta(seconds=run.budget_limits.timeout_seconds)
        if change.retry_at is not None and change.retry_at <= at:
            raise ValueError("retry_at must be in the future")
        connection.execute(
            """UPDATE runs SET status = ?, updated_at = ?, started_at = ?, deadline_at = ?,
                retry_at = ?, stop_reason = ? WHERE id = ? AND user_id = ?""",
            (
                change.status,
                timestamp(at),
                timestamp(started) if started is not None else None,
                timestamp(deadline) if deadline is not None else None,
                timestamp(change.retry_at) if change.retry_at is not None else None,
                change.stop_reason,
                run.id,
                run.user_id,
            ),
        )
        self._append_event(
            connection,
            run.id,
            "state_changed",
            at,
            {
                "from": run.status,
                "to": change.status,
                "stop_reason": change.stop_reason,
                "retry_at": timestamp(change.retry_at) if change.retry_at is not None else None,
            },
        )
        return _record(self._owned_row(connection, run.id, run.user_id))

    def consume_budget(
        self, run_id: str, user_id: str, charge: BudgetCharge, *, now: datetime | None = None
    ) -> RunRecord:
        """Persist requested counters together before the caller executes any action."""
        charge = BudgetCharge.model_validate(charge)
        with self.database.transaction() as connection:
            run = _record(self._owned_row(connection, run_id, user_id))
            return self._consume_budget(connection, run, charge, self.now(now))

    def _consume_budget(
        self, connection: sqlite3.Connection, run: RunRecord, charge: BudgetCharge, at: datetime
    ) -> RunRecord:
        """Shared storage primitive: reserve counters and start a call in one transaction."""
        self._check_clock(run, at)
        if run.status != RunStatus.RUNNING:
            raise InvalidRunStateError("budget can only be consumed by a running run")
        if at >= run.deadline_at:
            raise BudgetExceededError("deadline")
        usage = run.budget_usage.model_dump()
        for name, amount in charge.model_dump().items():
            usage[name] += amount
            if usage[name] > getattr(run.budget_limits, name):
                raise BudgetExceededError(name)
        connection.execute(
            """UPDATE runs SET budget_usage_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ?""",
            (BudgetUsage(**usage).model_dump_json(), timestamp(at), run.id, run.user_id),
        )
        self._append_event(
            connection,
            run.id,
            "budget_consumed",
            at,
            {"charge": charge.model_dump(), "usage": usage},
        )
        return _record(self._owned_row(connection, run.id, run.user_id))

    def list_events(
        self, run_id: str, user_id: str, *, after_id: int = 0, limit: int = 100
    ) -> list[RunEvent]:
        page = EventPage(after_id=after_id, limit=limit)
        with self.database.connection() as connection:
            self._owned_row(connection, run_id, user_id)
            rows = connection.execute(
                """SELECT * FROM run_events WHERE run_id = ? AND id > ?
                    ORDER BY id LIMIT ?""",
                (run_id, page.after_id, page.limit),
            ).fetchall()
            return [
                RunEvent(
                    id=row["id"],
                    run_id=row["run_id"],
                    kind=row["kind"],
                    occurred_at=row["occurred_at"],
                    data=self.projection.redactor.clean(json.loads(row["data_json"])),
                )
                for row in rows
            ]
