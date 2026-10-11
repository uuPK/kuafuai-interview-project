"""Atomically persist responses/calls and coordinate result, feedback and completion."""

import json
import sqlite3
from datetime import datetime

from app.agent.prompts import MISSING_SUBMISSION_PROMPT
from app.agent.protocol import (
    MESSAGE_ADAPTER,
    AssistantMessage,
    InputMessage,
    Message,
    ModelResponse,
    ToolMessage,
)
from app.harness.failures import FATAL_CATEGORIES, FailureInfo
from app.harness.guardrails import GuardrailStore
from app.storage.calls import CallRecord, CallStatus, CallStore, _call_record
from app.storage.models import RunRecord, RunStatus, StateChange, timestamp
from app.storage.requests import RequestStore
from app.storage.runs import InvalidRunStateError, _record
from app.tools.models import ToolInvocation
from app.tools.registry import CompletionStatus


class ConversationConflictError(ValueError):
    """A response or feedback would break the persisted conversation protocol."""


class ConversationStore:
    def __init__(self, calls: CallStore) -> None:
        self.calls = calls
        self.runs = calls.runs
        self.database = calls.database
        self.requests = RequestStore(calls)
        self.guards = GuardrailStore(self.runs)

    @staticmethod
    def _append_message(
        connection: sqlite3.Connection,
        run_id: str,
        message: Message,
        at: datetime,
        *,
        purpose: str | None = None,
        turn: int | None = None,
    ) -> None:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_messages WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO run_messages (
                run_id, seq, role, purpose, response_turn, tool_call_id, message_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                seq,
                message.role,
                purpose,
                turn,
                message.tool_call_id if isinstance(message, ToolMessage) else None,
                message.model_dump_json(),
                timestamp(at),
            ),
        )

    @staticmethod
    def _unpaired(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT c.*, m.turn, m.position FROM model_tool_calls m
                JOIN tool_calls c USING (run_id, tool_call_id)
                LEFT JOIN run_messages f ON f.run_id = m.run_id
                    AND f.tool_call_id = m.tool_call_id AND f.role = 'tool'
                WHERE m.run_id = ? AND f.seq IS NULL ORDER BY m.turn, m.position""",
            (run_id,),
        ).fetchall()

    def initialize(
        self, run_id: str, user_id: str, messages: tuple[InputMessage, InputMessage]
    ) -> None:
        messages = tuple(InputMessage.model_validate(message) for message in messages)
        if [message.role for message in messages] != ["system", "user"]:
            raise ValueError("initial conversation requires system and user messages")
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            if run.status != RunStatus.RUNNING:
                raise InvalidRunStateError("conversation requires a running run")
            if connection.execute(
                "SELECT 1 FROM run_messages WHERE run_id = ?", (run_id,)
            ).fetchone():
                return
            at = self.runs.now()
            for message in messages:
                self._append_message(connection, run_id, message, at, purpose="initial")
            self.runs._append_event(connection, run_id, "conversation_started", at, {})

    def messages(self, run_id: str, user_id: str) -> tuple[Message, ...]:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            return tuple(
                MESSAGE_ADAPTER.validate_json(row["message_json"])
                for row in connection.execute(
                    "SELECT message_json FROM run_messages WHERE run_id = ? ORDER BY seq", (run_id,)
                )
            )

    def responses(self, run_id: str, user_id: str) -> tuple[ModelResponse, ...]:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            return tuple(
                ModelResponse.model_validate_json(row["response_json"])
                for row in connection.execute(
                    "SELECT response_json FROM model_responses WHERE run_id = ? ORDER BY turn",
                    (run_id,),
                )
            )

    def save_response(
        self, run_id: str, user_id: str, response: ModelResponse, *, request_id: str | None = None
    ) -> int:
        response = ModelResponse.model_validate(response)
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            if run.status != RunStatus.RUNNING:
                raise InvalidRunStateError("response requires a running run")
            if self._unpaired(connection, run_id):
                raise ConversationConflictError("previous response still needs tool feedback")
            if not connection.execute(
                "SELECT 1 FROM run_messages WHERE run_id = ?", (run_id,)
            ).fetchone():
                raise ConversationConflictError("initial messages have not been saved")
            invocations = [call.invocation() for call in response.message.tool_calls]
            for call in invocations:
                if connection.execute(
                    "SELECT 1 FROM tool_calls WHERE run_id = ? AND tool_call_id = ?",
                    (run_id, call.tool_call_id),
                ).fetchone():
                    raise ConversationConflictError("new responses require new tool call IDs")
            turn = connection.execute(
                "SELECT COALESCE(MAX(turn), 0) + 1 FROM model_responses WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            at = self.runs.now()
            if request_id is not None:
                request = self.requests._owned(connection, request_id, user_id)
                if (
                    request.run_id != run_id
                    or request.kind != "model"
                    or request.status != "executing"
                ):
                    raise ConversationConflictError(
                        "response must belong to an executing model request"
                    )
                self.requests._complete(connection, request_id, user_id)
            connection.execute(
                """INSERT INTO model_responses
                    (run_id, turn, response_json, created_at, request_id) VALUES (?, ?, ?, ?, ?)""",
                (run_id, turn, response.model_dump_json(), timestamp(at), request_id),
            )
            self.calls._prepare_calls(connection, run_id, user_id, invocations)
            for position, call in enumerate(invocations):
                connection.execute(
                    "INSERT INTO model_tool_calls VALUES (?, ?, ?, ?)",
                    (run_id, turn, position, call.tool_call_id),
                )
            self._append_message(connection, run_id, response.message, at, turn=turn)
            self.runs._append_event(
                connection,
                run_id,
                "model_response_saved",
                at,
                {"turn": turn, "tool_calls": len(invocations)},
            )
            expected = "tool_calls" if invocations else "stop"
            if response.finish_reason != expected:
                self.runs._transition(
                    connection,
                    run,
                    StateChange(status="failed", stop_reason="model_incomplete_response"),
                    at,
                )
            return turn

    def pending_calls(self, run_id: str, user_id: str) -> tuple[ToolInvocation, ...]:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            return tuple(
                ToolInvocation(
                    tool_call_id=row["tool_call_id"], name=row["name"], arguments=row["arguments"]
                )
                for row in self._unpaired(connection, run_id)
            )

    def record_feedback(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        call: CallRecord,
        *,
        completion_status: CompletionStatus | None = None,
    ) -> None:
        """Called inside finish_call's transaction; no network or external writes here."""
        run = _record(self.runs._owned_row(connection, call.run_id, user_id))
        saved = _call_record(self.calls._row(connection, call.run_id, call.tool_call_id))
        if saved != call or call.status != CallStatus.COMPLETED or call.result is None:
            raise ConversationConflictError("only a durable final result can be paired")
        message = ToolMessage(tool_call_id=call.tool_call_id, content=call.result.model_dump_json())
        existing = connection.execute(
            "SELECT message_json FROM run_messages WHERE run_id = ? AND tool_call_id = ?",
            (call.run_id, call.tool_call_id),
        ).fetchone()
        if existing:
            if existing["message_json"] != message.model_dump_json():
                raise ConversationConflictError("saved feedback cannot be changed")
            return
        pending = self._unpaired(connection, call.run_id)
        if not pending or pending[0]["tool_call_id"] != call.tool_call_id:
            raise ConversationConflictError(
                "feedback requires an associated call in response order"
            )
        if completion_status is not None and (
            completion_status not in {"completed", "no_news"} or not call.result.ok
        ):
            raise ConversationConflictError("only accepted submissions may complete a run")
        at = self.runs.now()
        if completion_status is not None and run.status == RunStatus.RUNNING:
            self.runs._transition(connection, run, StateChange(status=completion_status), at)
        self._append_message(connection, call.run_id, message, at, turn=pending[0]["turn"])
        self.runs._append_event(
            connection,
            call.run_id,
            "tool_feedback_saved",
            at,
            {"tool_call_id": call.tool_call_id, "ok": call.result.ok},
        )
        reason = None
        if call.result.error:
            code = call.result.error.code
            request_row = connection.execute(
                """SELECT failure_json, stop_reason FROM external_requests
                    WHERE run_id = ? AND tool_call_id = ?""",
                (call.run_id, call.tool_call_id),
            ).fetchone()
            if code == "REPEATED_ACTION_STOPPED":
                reason = "repeated_failed_action"
            elif code == "BUDGET_EXCEEDED":
                reason = (
                    "budget_deadline"
                    if (request_row and request_row["stop_reason"] == "retry_deadline")
                    or at >= run.deadline_at
                    else "tool_budget_exceeded"
                )
            elif request_row and request_row["failure_json"]:
                failure = FailureInfo.model_validate_json(request_row["failure_json"])
                if failure.category in FATAL_CATEGORIES:
                    reason = f"tool_dependency_{failure.category}"
        current = _record(self.runs._owned_row(connection, call.run_id, user_id))
        if reason is not None and current.status == "running":
            self.runs._transition(
                connection, current, StateChange(status="failed", stop_reason=reason), at
            )

    def pair_saved_result(
        self,
        run_id: str,
        user_id: str,
        call_id: str,
        *,
        completion_status: CompletionStatus | None = None,
    ) -> None:
        """Reuse a final result that predates feedback; never re-execute or charge it."""
        with self.database.transaction() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            call = _call_record(self.calls._row(connection, run_id, call_id))
            self.record_feedback(connection, user_id, call, completion_status=completion_status)

    def check_submission(self, run_id: str, user_id: str) -> RunRecord:
        """Persist the single reminder, or fail after a subsequent text-only response."""
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            row = connection.execute(
                "SELECT message_json FROM run_messages WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if row is None or run.status != RunStatus.RUNNING:
                return run
            last = MESSAGE_ADAPTER.validate_json(row["message_json"])
            if not isinstance(last, AssistantMessage) or last.tool_calls:
                return run
            at = self.runs.now()
            if connection.execute(
                "SELECT 1 FROM run_messages WHERE run_id = ? AND purpose = 'missing_submission'",
                (run_id,),
            ).fetchone():
                return self.runs._transition(
                    connection,
                    run,
                    StateChange(status="failed", stop_reason="missing_submission"),
                    at,
                )
            self._append_message(
                connection,
                run_id,
                InputMessage(role="user", content=MISSING_SUBMISSION_PROMPT),
                at,
                purpose="missing_submission",
            )
            self.runs._append_event(connection, run_id, "submission_reminder", at, {})
            return run

    def flush_notices(self, run_id: str, user_id: str, request_timeout: float) -> None:
        """Append server guidance only after all IDs in a response have feedback."""
        with self.database.transaction() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            if run.status != "running" or self._unpaired(connection, run_id):
                return
            if self.guards.close_to_limit(run, request_timeout):
                remaining = {
                    name: max(0, getattr(run.budget_limits, name) - amount)
                    for name, amount in run.budget_usage.model_dump().items()
                }
                self._notice(
                    connection,
                    run_id,
                    "budget",
                    "budget_warning",
                    "预算接近上限，请优先形成可提交稿件；最后一次模型决策和一个工具名额用于提交。"
                    + json.dumps(
                        {"remaining": remaining, "deadline_at": run.deadline_at.isoformat()},
                        ensure_ascii=False,
                    ),
                )
            streak = self.guards.streak(connection, run_id)
            if streak and streak.count >= 3:
                self._notice(
                    connection,
                    run_id,
                    f"repeat:{streak.last_call_id}",
                    "repeat_warning",
                    f"同一动作已连续三次失败（最近反馈：{streak.error_code}）。"
                    "请改词、换来源或调整方案；再次重复该动作会结束任务。",
                )

    def _notice(self, connection, run_id: str, key: str, purpose: str, content: str) -> None:
        if self.guards.has_notice(connection, run_id, key):
            return
        at = self.runs.now()
        self._append_message(
            connection, run_id, InputMessage(role="user", content=content), at, purpose=purpose
        )
        connection.execute(
            """INSERT INTO harness_notices (run_id, notice_key, message_seq)
                SELECT ?, ?, MAX(seq) FROM run_messages WHERE run_id = ?""",
            (run_id, key, run_id),
        )
        self.runs._append_event(connection, run_id, purpose, at, {})

    def note_reserved_decision(self, run_id: str, user_id: str, request_id: str) -> None:
        with self.database.transaction() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            request = self.requests._owned(connection, request_id, user_id)
            if (
                request.run_id != run_id
                or request.kind != "model"
                or request.stop_reason != "decision_reserved"
            ):
                raise ConversationConflictError("notice requires the run's reserved model decision")
            self.requests._complete(connection, request_id, user_id)
            self._notice(
                connection,
                run_id,
                f"retry_reserved:{request_id}",
                "retry_notice",
                "本次模型请求临时失败，已停止重试并保留最后一次决策机会。请据已有材料调用提交工具。",
            )
