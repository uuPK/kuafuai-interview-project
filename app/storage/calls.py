"""Durable tool calls and compare-and-set execution with atomic budget reservation."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime

from app.storage.models import BudgetCharge, StoredModel, timestamp
from app.storage.runs import RunStore, _record
from app.tools.models import ToolInvocation, ToolResult


class CallStatus(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"


class CallRecord(StoredModel):
    run_id: str
    tool_call_id: str
    name: str
    arguments: str
    invocation_hash: str
    status: CallStatus
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    result: ToolResult | None


class CallConflictError(ValueError):
    """A saved ID must never be reused with a different invocation or result."""


class CallInProgressError(RuntimeError):
    """An executing call needs recovery or its original executor; do not execute it twice."""


def _call_record(row: sqlite3.Row) -> CallRecord:
    values = dict(row)
    encoded = values.pop("result_json")
    values["result"] = ToolResult.model_validate_json(encoded) if encoded is not None else None
    return CallRecord.model_validate(values)


class CallStore:
    def __init__(self, runs: RunStore) -> None:
        self.runs = runs
        self.database = runs.database

    @staticmethod
    def _row(connection: sqlite3.Connection, run_id: str, call_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? AND tool_call_id = ?", (run_id, call_id)
        ).fetchone()
        if row is None:
            raise LookupError("tool call not found")
        return row

    def prepare_calls(
        self, run_id: str, user_id: str, invocations: list[ToolInvocation]
    ) -> list[CallRecord]:
        with self.database.transaction() as connection:
            return self._prepare_calls(connection, run_id, user_id, invocations)

    def _prepare_calls(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        user_id: str,
        invocations: list[ToolInvocation],
    ) -> list[CallRecord]:
        """Share the transaction that saves a full model response and its call list."""
        calls = [ToolInvocation.model_validate(call) for call in invocations]
        if len({call.tool_call_id for call in calls}) != len(calls):
            raise CallConflictError("duplicate tool call IDs in response")
        self.runs._owned_row(connection, run_id, user_id)
        for call in calls:
            existing = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? AND tool_call_id = ?",
                (run_id, call.tool_call_id),
            ).fetchone()
            if existing is not None:
                if existing["invocation_hash"] != call.fingerprint:
                    raise CallConflictError("tool call ID conflicts with its saved invocation")
                continue
            at = datetime.now(UTC)
            connection.execute(
                """INSERT INTO tool_calls (
                    run_id, tool_call_id, name, arguments, invocation_hash, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    run_id,
                    call.tool_call_id,
                    call.name,
                    call.arguments,
                    call.fingerprint,
                    timestamp(at),
                ),
            )
            self.runs._append_event(
                connection,
                run_id,
                "tool_pending",
                at,
                {
                    "tool_call_id": call.tool_call_id,
                    "name": call.name,
                    "arguments_chars": len(call.arguments),
                },
            )
        return [_call_record(self._row(connection, run_id, call.tool_call_id)) for call in calls]

    def get_call(self, run_id: str, user_id: str, call_id: str) -> CallRecord:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            return _call_record(self._row(connection, run_id, call_id))

    def start_call(
        self, run_id: str, user_id: str, call_id: str, charge: BudgetCharge
    ) -> CallRecord:
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            call = _call_record(self._row(connection, run_id, call_id))
            if call.status != CallStatus.PENDING:
                raise CallInProgressError("tool call is not pending")
            at = datetime.now(UTC)
            self.runs._consume_budget(connection, run, BudgetCharge.model_validate(charge), at)
            connection.execute(
                """UPDATE tool_calls SET status = 'executing', started_at = ?
                    WHERE run_id = ? AND tool_call_id = ?""",
                (timestamp(at), run_id, call_id),
            )
            self.runs._append_event(
                connection,
                run_id,
                "tool_executing",
                at,
                {
                    "tool_call_id": call_id,
                    "name": call.name,
                },
            )
            return _call_record(self._row(connection, run_id, call_id))

    def finish_call(
        self,
        run_id: str,
        user_id: str,
        call_id: str,
        result: ToolResult,
        *,
        on_finished: Callable[[sqlite3.Connection, CallRecord], None] | None = None,
    ) -> CallRecord:
        result = ToolResult.model_validate(result)
        with self.database.transaction() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            call = _call_record(self._row(connection, run_id, call_id))
            if call.status == CallStatus.COMPLETED:
                if call.result != result:
                    raise CallConflictError("completed tool result cannot be changed")
                return call
            if call.status == CallStatus.PENDING and result.ok:
                raise CallConflictError("unexecuted calls cannot have successful results")
            at = datetime.now(UTC)
            connection.execute(
                """UPDATE tool_calls SET status = 'completed', finished_at = ?, result_json = ?
                    WHERE run_id = ? AND tool_call_id = ?""",
                (timestamp(at), result.model_dump_json(), run_id, call_id),
            )
            self.runs._append_event(
                connection,
                run_id,
                "tool_completed",
                at,
                {
                    "tool_call_id": call_id,
                    "name": call.name,
                    "ok": result.ok,
                    "error_code": result.error.code if result.error else None,
                    "result_chars": len(result.model_dump_json()),
                },
            )
            completed = _call_record(self._row(connection, run_id, call_id))
            if on_finished is not None:
                on_finished(connection, completed)
            return completed
