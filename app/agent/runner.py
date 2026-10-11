"""Let the model choose actions; persist observations before the next model request."""

import asyncio
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from app.agent.prompts import initial_messages
from app.agent.protocol import ModelClient, ModelFailure, ModelRequest, ModelResponse
from app.config import Settings
from app.storage.calls import CallConflictError, CallRecord, CallStatus
from app.storage.conversations import ConversationConflictError, ConversationStore
from app.storage.models import BudgetCharge, RunRecord, RunStatus, StateChange
from app.storage.runs import BudgetExceededError
from app.tools.executor import ToolExecutor
from app.tools.models import ToolResult
from app.tools.registry import CompletionStatus

TERMINAL = {RunStatus.COMPLETED, RunStatus.NO_NEWS, RunStatus.FAILED}


class RecoveryRequiredError(RuntimeError):
    """An executing call has an uncertain outcome; a recovery worker must resolve it."""


class AgentRunner:
    def __init__(
        self,
        model: ModelClient,
        executor: ToolExecutor,
        conversations: ConversationStore,
        settings: Settings,
    ) -> None:
        if conversations.calls is not executor.calls:
            raise ValueError("runner and executor must share the same call store")
        self.model = model
        self.executor = executor
        self.conversations = conversations
        self.runs = conversations.runs
        self.settings = settings

    def _completion(self, call: CallRecord) -> CompletionStatus | None:
        definition = self.executor.registry.get(call.name)
        if definition is not None and definition.completion_status is not None:
            return definition.completion_status(call.result)
        return None

    def _feedback(self, user_id: str):
        def save(connection: sqlite3.Connection, call: CallRecord) -> None:
            self.conversations.record_feedback(
                connection, user_id, call, completion_status=self._completion(call)
            )

        return save

    def _fail(self, run_id: str, user_id: str, reason: str) -> RunRecord:
        return self.runs.transition(
            run_id, user_id, StateChange(status="failed", stop_reason=reason)
        )

    async def _finish_pending(self, run_id: str, user_id: str) -> None:
        for invocation in self.conversations.pending_calls(run_id, user_id):
            record = self.executor.calls.get_call(run_id, user_id, invocation.tool_call_id)
            if record.status == CallStatus.EXECUTING:
                raise RecoveryRequiredError("executing tool call needs recovery")
            run = self.runs.get_run(run_id, user_id)
            if run.status == RunStatus.WAITING_RETRY:
                return
            if record.status == CallStatus.PENDING:
                if run.status in TERMINAL:
                    self.executor.calls.finish_call(
                        run_id,
                        user_id,
                        invocation.tool_call_id,
                        ToolResult.failure("RUN_FINISHED", "任务已结束，该调用未执行。"),
                        on_finished=self._feedback(user_id),
                    )
                else:
                    try:
                        await self.executor.execute(
                            run_id, user_id, invocation, on_finished=self._feedback(user_id)
                        )
                    except BudgetExceededError as error:
                        # A submission may hit the deadline while its transaction is committing.
                        self.executor.calls.finish_call(
                            run_id,
                            user_id,
                            invocation.tool_call_id,
                            ToolResult.failure("BUDGET_EXCEEDED", "任务截止时间已到，提交未保存。"),
                            on_finished=self._feedback(user_id),
                        )
                        self._fail(run_id, user_id, f"budget_{error.dimension}")
            current = self.executor.calls.get_call(run_id, user_id, invocation.tool_call_id)
            if current.status != CallStatus.COMPLETED:
                raise RecoveryRequiredError("tool result is not final yet")
            self.conversations.pair_saved_result(
                run_id,
                user_id,
                invocation.tool_call_id,
                completion_status=self._completion(current),
            )
            if (
                current.result.error is not None
                and current.result.error.code == "BUDGET_EXCEEDED"
                and self.runs.get_run(run_id, user_id).status == RunStatus.RUNNING
            ):
                self._fail(run_id, user_id, "tool_budget_exceeded")

    async def run(self, run_id: str, user_id: str) -> RunRecord:
        run = self.runs.get_run(run_id, user_id)
        if run.status == RunStatus.WAITING_RETRY:
            return run
        if run.status == RunStatus.QUEUED:
            run = self.runs.transition(run_id, user_id, StateChange(status="running"))
        if run.status == RunStatus.RUNNING:
            self.conversations.initialize(run_id, user_id, initial_messages(run))

        while True:
            # Drain a saved response before asking for any new model decision.
            await self._finish_pending(run_id, user_id)
            run = self.conversations.check_submission(run_id, user_id)
            if run.status != RunStatus.RUNNING:
                return run
            request = ModelRequest(
                messages=self.conversations.messages(run_id, user_id),
                tools=tuple(self.executor.registry.schemas()),
            )
            if len(request.model_dump_json()) > self.settings.max_model_input_chars:
                return self._fail(run_id, user_id, "model_input_too_large")
            try:
                run = self.runs.consume_budget(run_id, user_id, BudgetCharge(model_requests=1))
            except BudgetExceededError as error:
                return self._fail(run_id, user_id, f"budget_{error.dimension}")
            try:
                remaining = (run.deadline_at - datetime.now(UTC)).total_seconds()
                response = ModelResponse.model_validate(
                    await asyncio.wait_for(
                        self.model.complete(request),
                        timeout=max(0, min(self.settings.request_timeout_seconds, remaining)),
                    )
                )
            except ValidationError:
                return self._fail(run_id, user_id, "model_protocol_error")
            except TimeoutError:
                return self._fail(run_id, user_id, "model_timeout")
            except ModelFailure:
                return self._fail(run_id, user_id, "model_dependency_error")
            except Exception:
                return self._fail(run_id, user_id, "model_internal_error")
            try:
                self.conversations.save_response(run_id, user_id, response)
            except (ConversationConflictError, CallConflictError):
                return self._fail(run_id, user_id, "model_protocol_error")
            expected = "tool_calls" if response.message.tool_calls else "stop"
            if response.finish_reason != expected:
                self._fail(run_id, user_id, "model_incomplete_response")
                # Keep every ID paired even when a truncated response is rejected.
                await self._finish_pending(run_id, user_id)
                return self.runs.get_run(run_id, user_id)
