"""Durable per-request attempts; reserve counters before any external work starts."""

import sqlite3
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from app.config import Settings
from app.harness.failures import FailureInfo, RequestInProgressError, RetryNotDueError
from app.harness.retry import retry_delay
from app.storage.calls import CallRecord, CallStore
from app.storage.models import BudgetCharge, StateChange, StoredModel, timestamp
from app.storage.runs import BudgetExceededError, InvalidRunStateError, _record
from app.tools.models import ToolResult


class RequestRecord(StoredModel):
    id: str
    run_id: str
    kind: Literal["model", "tool"]
    tool_call_id: str | None
    payload_json: str = Field(repr=False)
    status: Literal["ready", "executing", "waiting", "completed"]
    attempts: int
    max_attempts: int
    retry_base_seconds: float
    retry_at: AwareDatetime | None
    failure: FailureInfo | None = None
    feedback: ToolResult | None = None
    stop_reason: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


def _request(row: sqlite3.Row) -> RequestRecord:
    values = dict(row)
    failure = values.pop("failure_json")
    feedback = values.pop("feedback_json")
    values["failure"] = FailureInfo.model_validate_json(failure) if failure else None
    values["feedback"] = ToolResult.model_validate_json(feedback) if feedback else None
    return RequestRecord.model_validate(values)


class RequestStore:
    def __init__(self, calls: CallStore) -> None:
        self.calls, self.runs, self.database = calls, calls.runs, calls.database

    def _owned(self, connection, request_id, user_id) -> RequestRecord:
        row = connection.execute(
            """SELECT e.* FROM external_requests e JOIN runs r ON r.id = e.run_id
                WHERE e.id = ? AND r.user_id = ?""",
            (request_id, user_id),
        ).fetchone()
        if row is None:
            from app.storage.runs import RunNotFoundError

            raise RunNotFoundError("request not found")
        return _request(row)

    def get(self, request_id: str, user_id: str) -> RequestRecord:
        with self.database.connection() as connection:
            return self._owned(connection, request_id, user_id)

    def active_model(self, run_id: str, user_id: str) -> RequestRecord | None:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            row = connection.execute(
                """SELECT * FROM external_requests
                    WHERE run_id = ? AND kind = 'model' AND status != 'completed'""",
                (run_id,),
            ).fetchone()
            return _request(row) if row else None

    def for_tool(self, run_id: str, user_id: str, call_id: str) -> RequestRecord | None:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            row = connection.execute(
                "SELECT * FROM external_requests WHERE run_id = ? AND tool_call_id = ?",
                (run_id, call_id),
            ).fetchone()
            return _request(row) if row else None

    def prepare(
        self,
        run_id: str,
        user_id: str,
        kind: Literal["model", "tool"],
        payload_json: str,
        settings: Settings,
        *,
        call_id: str | None = None,
        retry_safe: bool = True,
    ) -> RequestRecord:
        if (kind == "tool") != (call_id is not None) or kind not in {"model", "tool"}:
            raise ValueError("request kind and tool call must match")
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            if run.status != "running":
                raise InvalidRunStateError("requests require a running run")
            if kind == "tool":
                self.calls._row(connection, run_id, call_id)
                existing = connection.execute(
                    "SELECT * FROM external_requests WHERE run_id = ? AND tool_call_id = ?",
                    (run_id, call_id),
                ).fetchone()
            else:
                existing = connection.execute(
                    """SELECT * FROM external_requests
                    WHERE run_id = ? AND kind = 'model' AND status != 'completed'""",
                    (run_id,),
                ).fetchone()
            if existing:
                record = _request(existing)
                if record.payload_json != payload_json:
                    raise ValueError("an existing request payload cannot be replaced")
                return record
            at, request_id = self.runs.now(), str(uuid4())
            connection.execute(
                """INSERT INTO external_requests (
                    id, run_id, kind, tool_call_id, payload_json, status, max_attempts,
                    retry_base_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
                (
                    request_id,
                    run_id,
                    kind,
                    call_id,
                    payload_json,
                    settings.external_max_attempts if retry_safe else 1,
                    settings.retry_base_seconds,
                    timestamp(at),
                    timestamp(at),
                ),
            )
            self.runs._append_event(
                connection,
                run_id,
                "request_prepared",
                at,
                {
                    "request_id": request_id,
                    "kind": kind,
                    "max_attempts": settings.external_max_attempts if retry_safe else 1,
                },
            )
            return self._owned(connection, request_id, user_id)

    def start(
        self, request_id: str, user_id: str, *, tool_dimension: str = "tool_calls"
    ) -> RequestRecord:
        with self.database.transaction() as connection:
            request = self._owned(connection, request_id, user_id)
            run = _record(self.runs._owned_row(connection, request.run_id, user_id))
            at = self.runs.now()
            if request.status in {"executing", "completed"}:
                raise RequestInProgressError("request is not available for execution")
            if run.status != "running":
                raise InvalidRunStateError("attempt requires a running run")
            if at >= run.deadline_at:
                raise BudgetExceededError("deadline")
            if request.retry_at is not None and at < request.retry_at:
                raise RetryNotDueError("retry time has not arrived")
            if request.attempts >= request.max_attempts:
                raise RequestInProgressError("attempt limit reached")
            if request.kind == "model":
                self.runs._consume_budget(connection, run, BudgetCharge(model_requests=1), at)
            elif request.attempts == 0:
                charge = {"tool_calls": 1}
                if tool_dimension not in {"tool_calls", "searches", "fetches"}:
                    raise ValueError("unsupported tool budget dimension")
                if tool_dimension != "tool_calls":
                    charge[tool_dimension] = 1
                self.calls._start_call(
                    connection,
                    request.run_id,
                    user_id,
                    request.tool_call_id,
                    BudgetCharge(**charge),
                    at=at,
                )
            connection.execute(
                """UPDATE external_requests SET attempts = attempts + 1, status = 'executing',
                    retry_at = NULL, updated_at = ? WHERE id = ?""",
                (timestamp(at), request.id),
            )
            self.runs._append_event(
                connection,
                run.id,
                "request_attempt_started",
                at,
                {"request_id": request.id, "kind": request.kind, "attempt": request.attempts + 1},
            )
            return self._owned(connection, request.id, user_id)

    def fail_attempt(
        self,
        request_id: str,
        user_id: str,
        failure: FailureInfo,
        settings: Settings,
        *,
        feedback: ToolResult | None = None,
        failure_stop_reason: str | None = None,
    ) -> RequestRecord:
        failure = FailureInfo.model_validate(failure)
        with self.database.transaction() as connection:
            request = self._owned(connection, request_id, user_id)
            if request.status != "executing":
                raise RequestInProgressError("only an executing attempt can fail")
            run = _record(self.runs._owned_row(connection, request.run_id, user_id))
            at = self.runs.now()
            status, retry_at, reason = "completed", None, None
            can_retry = failure.retryable and request.attempts < request.max_attempts
            if at >= run.deadline_at:
                reason = "retry_deadline"
            elif can_retry:
                delay = retry_delay(
                    request.retry_base_seconds, request.attempts, failure.retry_after, at
                )
                if delay >= (run.deadline_at - at).total_seconds():
                    reason = "retry_deadline"
                elif (
                    request.kind == "model"
                    and run.budget_usage.model_requests >= run.budget_limits.model_requests
                ):
                    reason = "model_budget"
                else:
                    status, retry_at = "waiting", at + timedelta(seconds=delay)
                    if (
                        request.kind == "model"
                        and run.budget_limits.model_requests - run.budget_usage.model_requests == 1
                    ):
                        reason = "decision_reserved"
                    if delay > settings.retry_inline_max_seconds:
                        self.runs._transition(
                            connection,
                            run,
                            StateChange(status="waiting_retry", retry_at=retry_at),
                            at,
                        )
            elif failure.retryable:
                reason = "retry_exhausted"
            # Store only classification and the executor's sanitized feedback, never headers.
            safe_failure = failure.model_copy(update={"retry_after": None})
            connection.execute(
                """UPDATE external_requests SET status = ?, retry_at = ?, failure_json = ?,
                    feedback_json = ?, stop_reason = ?, updated_at = ? WHERE id = ?""",
                (
                    status,
                    timestamp(retry_at) if retry_at else None,
                    safe_failure.model_dump_json(),
                    feedback.model_dump_json() if feedback else None,
                    reason,
                    timestamp(at),
                    request.id,
                ),
            )
            self.runs._append_event(
                connection,
                run.id,
                "request_attempt_failed",
                at,
                {
                    "request_id": request.id,
                    "attempt": request.attempts,
                    "category": failure.category,
                    "retryable": failure.retryable,
                    "retry_at": timestamp(retry_at) if retry_at else None,
                    "stop_reason": reason,
                },
            )
            if (
                request.kind == "model"
                and status == "completed"
                and failure_stop_reason is not None
            ):
                final_reason = (
                    "budget_deadline"
                    if reason == "retry_deadline"
                    else "budget_model_requests"
                    if reason == "model_budget"
                    else failure_stop_reason
                )
                self.runs._transition(
                    connection, run, StateChange(status="failed", stop_reason=final_reason), at
                )
            return self._owned(connection, request.id, user_id)

    def _complete(self, connection, request_id, user_id) -> RequestRecord:
        request = self._owned(connection, request_id, user_id)
        at = self.runs.now()
        connection.execute(
            """UPDATE external_requests SET status = 'completed',
                retry_at = NULL, updated_at = ? WHERE id = ?""",
            (timestamp(at), request.id),
        )
        return self._owned(connection, request.id, user_id)

    def abandon(self, request_id: str, user_id: str) -> None:
        """Close a request rejected before work or before its response can be saved."""
        with self.database.transaction() as connection:
            self._complete(connection, request_id, user_id)

    def complete_tool(
        self, request_id, user_id, result, *, on_finished=None, stop_reason=None
    ) -> CallRecord:
        with self.database.transaction() as connection:
            request = self._owned(connection, request_id, user_id)
            if request.kind != "tool":
                raise ValueError("tool result requires a tool request")
            self._complete(connection, request.id, user_id)
            if stop_reason is not None:
                connection.execute(
                    "UPDATE external_requests SET stop_reason = ? WHERE id = ?",
                    (stop_reason, request.id),
                )
            return self.calls._finish_call(
                connection,
                request.run_id,
                user_id,
                request.tool_call_id,
                result,
                on_finished=on_finished,
            )

    def resume_due_run(self, run_id: str, user_id: str):
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            at = self.runs.now()
            if run.status != "waiting_retry" or at < run.retry_at:
                return run
            if not connection.execute(
                "SELECT 1 FROM external_requests WHERE run_id = ? AND status = 'waiting'", (run_id,)
            ).fetchone():
                return run
            if at >= run.deadline_at:
                connection.execute(
                    """UPDATE external_requests SET status = 'completed', retry_at = NULL,
                        stop_reason = 'retry_deadline', updated_at = ?
                        WHERE run_id = ? AND kind = 'model' AND status = 'waiting'""",
                    (timestamp(at), run_id),
                )
                return self.runs._transition(
                    connection, run, StateChange(status="failed", stop_reason="budget_deadline"), at
                )
            return self.runs._transition(connection, run, StateChange(status="running"), at)
