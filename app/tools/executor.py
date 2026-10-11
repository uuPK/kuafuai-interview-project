"""Validate, record, execute, sanitize and bound each call; no retries are implicit."""

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from app.config import Settings
from app.storage.calls import (
    CallConflictError,
    CallInProgressError,
    CallRecord,
    CallStatus,
    CallStore,
)
from app.storage.models import BudgetCharge, RunStatus
from app.storage.runs import BudgetExceededError, InvalidRunStateError, RunNotFoundError
from app.tools.context import bind_context
from app.tools.models import ArgumentIssue, ToolFailure, ToolInvocation, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition, ToolRegistry

SENSITIVE_KEYS = {"apikey", "password", "secret", "token", "authorization", "cookie", "setcookie"}


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
    def __init__(self, registry: ToolRegistry, calls: CallStore, settings: Settings) -> None:
        self.registry = registry
        self.calls = calls
        self.settings = settings
        self._secrets = tuple(
            value.get_secret_value()
            for name in (
                "deepseek_api_key",
                "bocha_api_key",
                "smtp_username",
                "smtp_password",
                "session_secret",
            )
            if (value := getattr(settings, name)) is not None
        )

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(
                r"(?im)\b(authorization|set-cookie|cookie)\s*[:=][^\r\n]+",
                r"\1: [REDACTED]",
                value,
            )
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {
                self._redact(key): "[REDACTED]"
                if re.sub(r"[^a-z]", "", key.lower()) in SENSITIVE_KEYS
                else self._redact(item)
                for key, item in value.items()
            }
        return value

    def _normalize(self, result: ToolResult) -> ToolResult:
        result = ToolResult.model_validate_json(
            json.dumps(
                self._redact(result.model_dump(mode="json")), ensure_ascii=False, allow_nan=False
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
        if record.status == CallStatus.EXECUTING:
            return ToolResult.failure(
                "CALL_IN_PROGRESS", "该调用尚未完成，需等待执行或恢复。", retryable=True
            )

        definition = self.registry.get(invocation.name)
        problem, parameters, context = None, None, None
        if definition is None:
            problem = ToolResult.failure("UNKNOWN_TOOL", "工具未注册，请使用已提供的工具。")
        else:
            try:
                parameters = self._arguments(definition, invocation)
                context = bind_context(self.calls.runs, self.settings.data_dir, run_id, user_id)
            except (ValidationError, ValueError, RecursionError) as error:
                problem = self._argument_error(definition, error)
            except (PermissionError, RunNotFoundError):
                problem = ToolResult.failure(
                    "PERMISSION_DENIED", "工具任务目录或所属用户校验失败。"
                )

        charge = {"tool_calls": 1}
        if problem is None and definition.budget_dimension != "tool_calls":
            charge[definition.budget_dimension] = 1
        try:
            self.calls.start_call(run_id, user_id, invocation.tool_call_id, BudgetCharge(**charge))
        except CallInProgressError:
            # Another executor may have completed between prepare and the execution claim.
            current = self.calls.get_call(run_id, user_id, invocation.tool_call_id)
            return (
                current.result
                if current.result is not None
                else ToolResult.failure(
                    "CALL_IN_PROGRESS", "该调用尚未完成，需等待执行或恢复。", retryable=True
                )
            )
        except BudgetExceededError as error:
            problem = ToolResult.failure("BUDGET_EXCEEDED", f"任务预算已耗尽：{error.dimension}。")
        except InvalidRunStateError:
            run = self.calls.runs.get_run(run_id, user_id)
            terminal = run.status in {RunStatus.COMPLETED, RunStatus.NO_NEWS, RunStatus.FAILED}
            problem = ToolResult.failure(
                "RUN_FINISHED" if terminal else "RUN_NOT_RUNNING", "任务已结束或暂未运行。"
            )
        else:
            if problem is None:
                try:
                    remaining = (context.deadline_at - datetime.now(UTC)).total_seconds()
                    result = await asyncio.wait_for(
                        definition.handler(context, parameters),
                        timeout=max(0, min(self.settings.request_timeout_seconds, remaining)),
                    )
                    problem = ToolResult.model_validate(result)
                except ToolFailure as error:
                    problem = error.result
                except TimeoutError:
                    problem = ToolResult.failure(
                        "TOOL_TIMEOUT", "工具请求超时，可稍后重试。", retryable=True
                    )
                except PermissionError:
                    problem = ToolResult.failure("PERMISSION_DENIED", "工具访问超出当前任务权限。")
                except Exception:
                    problem = ToolResult.failure(
                        "INTERNAL_TOOL_ERROR", "工具执行失败，请调整行动。"
                    )
        try:
            problem = self._normalize(problem)
        except (ValueError, RecursionError):
            problem = ToolResult.failure("INTERNAL_TOOL_ERROR", "工具结果格式不符合协议。")
        return self.calls.finish_call(
            run_id, user_id, invocation.tool_call_id, problem, on_finished=on_finished
        ).result
