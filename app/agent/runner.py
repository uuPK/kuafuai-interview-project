"""Let the model choose actions; persist observations before the next model request."""

import asyncio
import sqlite3

from pydantic import ValidationError

from app.agent.prompts import initial_messages
from app.agent.protocol import ModelClient, ModelFailure, ModelRequest, ModelResponse
from app.config import Settings
from app.harness.execution import LeaseLostError
from app.harness.failures import FailureInfo, RequestInProgressError, RetryDeferredError
from app.storage.calls import CallConflictError, CallRecord, CallStatus
from app.storage.conversations import ConversationConflictError, ConversationStore
from app.storage.models import RunRecord, RunStatus, StateChange
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
        self.requests = conversations.requests

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
            request = self.requests.for_tool(run_id, user_id, invocation.tool_call_id)
            if record.status == CallStatus.EXECUTING and (
                request is None or request.status == "executing"
            ):
                raise RecoveryRequiredError("executing tool call needs recovery")
            run = self.runs.get_run(run_id, user_id)
            if run.status == RunStatus.WAITING_RETRY:
                raise RetryDeferredError("tool retry is waiting")
            if record.status != CallStatus.COMPLETED:
                if run.status in TERMINAL:
                    expired = run.stop_reason == "budget_deadline"
                    self.executor.finish(
                        run_id,
                        user_id,
                        invocation,
                        ToolResult.failure(
                            "BUDGET_EXCEEDED" if expired else "RUN_FINISHED",
                            "任务已结束，该调用未继续执行。",
                        ),
                        on_finished=self._feedback(user_id),
                    )
                else:
                    try:
                        await self.executor.execute(
                            run_id, user_id, invocation, on_finished=self._feedback(user_id)
                        )
                    except BudgetExceededError:
                        # Result, feedback and deadline stop are saved together after rollback.
                        self.executor.finish(
                            run_id,
                            user_id,
                            invocation,
                            ToolResult.failure("BUDGET_EXCEEDED", "任务截止时间已到，提交未保存。"),
                            on_finished=self._feedback(user_id),
                            stop_reason="retry_deadline",
                        )
            current = self.executor.calls.get_call(run_id, user_id, invocation.tool_call_id)
            if current.status != CallStatus.COMPLETED:
                raise RecoveryRequiredError("tool result is not final yet")
            self.conversations.pair_saved_result(
                run_id,
                user_id,
                invocation.tool_call_id,
                completion_status=self._completion(current),
            )

    async def _ask_model(self, run_id: str, user_id: str):
        saved = self.requests.active_model(run_id, user_id)
        if saved is not None and saved.status == "executing":
            raise RecoveryRequiredError("executing model request needs recovery")
        run = self.runs.get_run(run_id, user_id)
        reserve = self.executor.registry.submission_model_reserve
        if run.budget_limits.model_requests - run.budget_usage.model_requests <= reserve:
            if saved is not None:
                self.requests.abandon(saved.id, user_id)
            self._fail(run_id, user_id, "budget_model_requests")
            return None
        if saved is None:
            self.conversations.flush_notices(
                run_id, user_id, self.settings.request_timeout_seconds, review_reserve=reserve
            )
            request = ModelRequest(
                messages=self.conversations.messages(run_id, user_id),
                tools=tuple(self.executor.registry.schemas()),
            )
            self.conversations.flush_notices(
                run_id,
                user_id,
                self.settings.request_timeout_seconds,
                input_chars=len(request.model_dump_json()),
                max_input_chars=self.settings.max_model_input_chars,
                review_reserve=reserve,
            )
            request = request.model_copy(
                update={"messages": self.conversations.messages(run_id, user_id)}
            )
        else:
            request = ModelRequest.model_validate_json(saved.payload_json)
        if len(request.model_dump_json()) > self.settings.max_model_input_chars:
            if saved is not None:
                self.requests.abandon(saved.id, user_id)
            self._fail(run_id, user_id, "model_input_too_large")
            return None
        saved = self.requests.prepare(
            run_id, user_id, "model", request.model_dump_json(), self.settings
        )
        while True:
            if saved.retry_at is not None:
                delay = (saved.retry_at - self.runs.now()).total_seconds()
                if delay > 0:
                    await self.executor.sleep(delay)
            if saved.stop_reason == "decision_reserved":
                if self.runs.now() >= self.runs.get_run(run_id, user_id).deadline_at:
                    self.requests.abandon(saved.id, user_id)
                    self._fail(run_id, user_id, "budget_deadline")
                else:
                    self.conversations.note_reserved_decision(run_id, user_id, saved.id)
                return None
            try:
                saved = self.requests.start(saved.id, user_id)
            except BudgetExceededError as error:
                self.requests.abandon(saved.id, user_id)
                self._fail(run_id, user_id, f"budget_{error.dimension}")
                return None
            except RequestInProgressError as error:
                raise RecoveryRequiredError("model request already claimed") from error
            run = self.runs.get_run(run_id, user_id)
            try:
                remaining = (run.deadline_at - self.runs.now()).total_seconds()
                response = ModelResponse.model_validate(
                    await asyncio.wait_for(
                        self.model.complete(request),
                        timeout=max(0, min(self.settings.request_timeout_seconds, remaining)),
                    )
                )
                return saved, response
            except ValidationError:
                failure, reason = (
                    FailureInfo(code="MODEL_PROTOCOL_ERROR", category="protocol"),
                    "model_protocol_error",
                )
            except TimeoutError:
                failure, reason = (
                    FailureInfo(code="MODEL_TIMEOUT", category="transient", retryable=True),
                    "model_timeout",
                )
            except ModelFailure as error:
                failure = error.info
                reason = (
                    f"model_dependency_{failure.category}"
                    if failure.category in {"authentication", "quota", "configuration", "protocol"}
                    else "model_dependency_error"
                )
            except LeaseLostError:
                raise
            except Exception:
                failure, reason = FailureInfo(code="MODEL_INTERNAL_ERROR"), "model_internal_error"
            saved = self.requests.fail_attempt(
                saved.id,
                user_id,
                failure,
                self.settings,
                failure_stop_reason=reason,
                reserve_model_requests=reserve,
            )
            if saved.status == "completed":
                return None
            if self.runs.get_run(run_id, user_id).status == RunStatus.WAITING_RETRY:
                raise RetryDeferredError("model retry saved for later")

    async def run(self, run_id: str, user_id: str) -> RunRecord:
        run = self.runs.get_run(run_id, user_id)
        if run.status == RunStatus.WAITING_RETRY:
            run = self.requests.resume_due_run(run_id, user_id)
            if run.status == RunStatus.WAITING_RETRY:
                return run
        if run.status == RunStatus.QUEUED:
            run = self.runs.transition(run_id, user_id, StateChange(status="running"))
        if run.status == RunStatus.RUNNING:
            self.conversations.initialize(
                run_id,
                user_id,
                initial_messages(run, available_feeds=self.executor.registry.available_feeds),
            )
        while True:
            try:
                await self._finish_pending(run_id, user_id)
                run = self.conversations.check_submission(run_id, user_id)
                if run.status != RunStatus.RUNNING:
                    return run
                decision = await self._ask_model(run_id, user_id)
            except RetryDeferredError:
                return self.runs.get_run(run_id, user_id)
            if decision is None:
                continue
            saved, response = decision
            try:
                self.conversations.save_response(run_id, user_id, response, request_id=saved.id)
            except (ConversationConflictError, CallConflictError):
                self.requests.abandon(saved.id, user_id)
                return self._fail(run_id, user_id, "model_protocol_error")
            expected = "tool_calls" if response.message.tool_calls else "stop"
            if response.finish_reason != expected:
                await self._finish_pending(run_id, user_id)
                return self.runs.get_run(run_id, user_id)
