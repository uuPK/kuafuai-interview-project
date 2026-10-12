"""Validate and persist logical calls; retry only trusted, explicitly safe operations."""

import asyncio
import json
import sqlite3
from collections.abc import Callable

from pydantic import ValidationError

from app.config import Settings
from app.harness.failures import (
    FATAL_CATEGORIES,
    FailureInfo,
    RequestInProgressError,
    RetryDeferredError,
)
from app.harness.guardrails import GuardrailStore
from app.harness.redaction import Redactor
from app.storage.calls import (
    CallConflictError,
    CallInProgressError,
    CallRecord,
    CallStatus,
    CallStore,
)
from app.storage.models import BudgetCharge, RunStatus
from app.storage.requests import RequestStore
from app.storage.runs import BudgetExceededError, RunNotFoundError
from app.tools.context import bind_context
from app.tools.models import ArgumentIssue, ToolFailure, ToolInvocation, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition, ToolRegistry


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, calls: CallStore, settings: Settings, *, sleep=asyncio.sleep
    ) -> None:
        self.registry = registry
        self.calls = calls
        self.settings = settings
        self.requests = RequestStore(calls)
        self.guards = GuardrailStore(calls.runs)
        self.sleep = sleep
        self.redactor = Redactor(settings)

    def _normalize(self, result: ToolResult) -> ToolResult:
        result = ToolResult.model_validate_json(
            json.dumps(
                self.redactor.clean(result.model_dump(mode="json")),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        if len(result.model_dump_json()) > self.settings.max_tool_result_chars:
            return ToolResult.failure("RESULT_TOO_LARGE", "工具结果过大，请缩小查询或分页读取。")
        return result

    def _arguments(self, definition: ToolDefinition, invocation: ToolInvocation) -> ToolParameters:
        if len(invocation.arguments) > self.settings.max_model_input_chars:
            raise ValueError("arguments exceed configured input limit")
        value = json.loads(
            invocation.arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(value, dict):
            raise ValueError("arguments must be a JSON object")
        return definition.parameters.model_validate_json(invocation.arguments)

    def _argument_error(self, definition: ToolDefinition, error: Exception) -> ToolResult:
        issues = ()
        if isinstance(error, ValidationError):
            issues = tuple(
                ArgumentIssue(
                    field=str(item["loc"][0])
                    if item["loc"] and item["loc"][0] in definition.parameters.model_fields
                    else "[unknown]",
                    issue=item["type"][:80],
                )
                for item in error.errors(
                    include_input=False, include_context=False, include_url=False
                )[:10]
            )
        return ToolResult.failure(
            "INVALID_ARGUMENTS", "参数不符合工具 schema，请检查字段与类型。", details=issues
        )

    def finish(
        self, run_id, user_id, invocation, result, *, on_finished=None, stop_reason=None
    ) -> ToolResult:
        request = self.requests.for_tool(run_id, user_id, invocation.tool_call_id)
        if request is not None:
            record = self.requests.complete_tool(
                request.id, user_id, result, on_finished=on_finished, stop_reason=stop_reason
            )
        else:
            record = self.calls.finish_call(
                run_id, user_id, invocation.tool_call_id, result, on_finished=on_finished
            )
        return record.result

    @staticmethod
    def _in_progress() -> ToolResult:
        return ToolResult.failure(
            "CALL_IN_PROGRESS", "该调用尚未完成，需等待执行或恢复。", retryable=True
        )

    async def execute(
        self,
        run_id: str,
        user_id: str,
        invocation: ToolInvocation,
        *,
        on_finished: Callable[[sqlite3.Connection, CallRecord], None] | None = None,
    ) -> ToolResult:
        invocation = ToolInvocation.model_validate(invocation)
        try:
            record = self.calls.prepare_calls(run_id, user_id, [invocation])[0]
        except RunNotFoundError:
            return ToolResult.failure("PERMISSION_DENIED", "任务不存在或不属于当前用户。")
        except CallConflictError:
            return ToolResult.failure("CALL_ID_CONFLICT", "调用 ID 与已保存的调用不一致。")
        if record.status == CallStatus.COMPLETED:
            return record.result
        request = self.requests.for_tool(run_id, user_id, invocation.tool_call_id)
        if record.status == CallStatus.EXECUTING and (
            request is None or request.status == "executing"
        ):
            return self._in_progress()
        if request is not None and request.status == "completed" and request.feedback is not None:
            result = request.feedback
            if request.stop_reason == "retry_deadline":
                result = ToolResult.failure("BUDGET_EXCEEDED", "重试无法在任务截止时间内完成。")
            return self.finish(run_id, user_id, invocation, result, on_finished=on_finished)
        run = self.calls.runs.get_run(run_id, user_id)
        if run.status == RunStatus.WAITING_RETRY and request is not None:
            raise RetryDeferredError("tool retry is waiting")
        if run.status != RunStatus.RUNNING:
            terminal = run.status in {RunStatus.COMPLETED, RunStatus.NO_NEWS, RunStatus.FAILED}
            return self.finish(
                run_id,
                user_id,
                invocation,
                ToolResult.failure(
                    "RUN_FINISHED" if terminal else "RUN_NOT_RUNNING", "任务已结束或暂未运行。"
                ),
                on_finished=on_finished,
            )

        definition = self.registry.get(invocation.name)
        if record.status == CallStatus.PENDING:
            guard = self.guards.before_new_call(
                run_id,
                user_id,
                invocation,
                is_submission=definition is not None and definition.completion_status is not None,
            )
            if guard is not None:
                return self.finish(run_id, user_id, invocation, guard, on_finished=on_finished)
        problem, parameters, context = None, None, None
        if definition is None:
            problem = ToolResult.failure("UNKNOWN_TOOL", "工具未注册，请使用已提供的工具。")
        else:
            try:
                parameters = self._arguments(definition, invocation)
                context = bind_context(
                    self.calls.runs,
                    self.settings.data_dir,
                    run_id,
                    user_id,
                    tool_call_id=invocation.tool_call_id,
                )
            except (ValidationError, ValueError, RecursionError) as error:
                problem = self._argument_error(definition, error)
            except (PermissionError, RunNotFoundError):
                problem = ToolResult.failure(
                    "PERMISSION_DENIED", "工具任务目录或所属用户校验失败。"
                )
        if problem is not None:
            if record.status == CallStatus.PENDING:
                try:
                    self.calls.start_call(
                        run_id, user_id, invocation.tool_call_id, BudgetCharge(tool_calls=1)
                    )
                except CallInProgressError:
                    current = self.calls.get_call(run_id, user_id, invocation.tool_call_id)
                    return current.result or self._in_progress()
                except BudgetExceededError as error:
                    problem = ToolResult.failure(
                        "BUDGET_EXCEEDED", f"任务预算已耗尽：{error.dimension}。"
                    )
            return self.finish(run_id, user_id, invocation, problem, on_finished=on_finished)

        request = self.requests.prepare(
            run_id,
            user_id,
            "tool",
            invocation.model_dump_json(),
            self.settings,
            call_id=invocation.tool_call_id,
            retry_safe=definition.retry_safe,
        )
        while True:
            if request.retry_at is not None:
                delay = (request.retry_at - self.calls.runs.now()).total_seconds()
                if delay > 0:
                    await self.sleep(delay)
            try:
                request = self.requests.start(
                    request.id, user_id, tool_dimension=definition.budget_dimension
                )
            except (RequestInProgressError, CallInProgressError):
                current = self.calls.get_call(run_id, user_id, invocation.tool_call_id)
                return current.result or self._in_progress()
            except BudgetExceededError as error:
                return self.finish(
                    run_id,
                    user_id,
                    invocation,
                    ToolResult.failure("BUDGET_EXCEEDED", f"任务预算已耗尽：{error.dimension}。"),
                    on_finished=on_finished,
                )
            failure = None
            try:
                remaining = (context.deadline_at - self.calls.runs.now()).total_seconds()
                result = ToolResult.model_validate(
                    await asyncio.wait_for(
                        definition.handler(context, parameters),
                        timeout=max(0, min(self.settings.request_timeout_seconds, remaining)),
                    )
                )
            except ToolFailure as error:
                result, failure = error.result, error.info
            except TimeoutError:
                result = ToolResult.failure(
                    "TOOL_TIMEOUT", "工具请求超时，可稍后重试。", retryable=True
                )
            except PermissionError:
                result = ToolResult.failure("PERMISSION_DENIED", "工具访问超出当前任务权限。")
            except Exception:
                result = ToolResult.failure("INTERNAL_TOOL_ERROR", "工具执行失败，请调整行动。")
            try:
                result = self._normalize(result)
            except (ValueError, RecursionError):
                result = ToolResult.failure("INTERNAL_TOOL_ERROR", "工具结果格式不符合协议。")
            if result.ok:
                return self.finish(run_id, user_id, invocation, result, on_finished=on_finished)
            if failure is None or (
                failure.code != result.error.code and failure.category not in FATAL_CATEGORIES
            ):
                failure = FailureInfo(
                    code=result.error.code,
                    category="transient" if result.error.retryable else "observation",
                    retryable=result.error.retryable,
                )
            request = self.requests.fail_attempt(
                request.id, user_id, failure, self.settings, feedback=result
            )
            if request.status == "completed":
                if request.stop_reason == "retry_deadline":
                    result = ToolResult.failure("BUDGET_EXCEEDED", "重试无法在任务截止时间内完成。")
                return self.finish(run_id, user_id, invocation, result, on_finished=on_finished)
            if self.calls.runs.get_run(run_id, user_id).status == RunStatus.WAITING_RETRY:
                raise RetryDeferredError("tool retry saved for later")
