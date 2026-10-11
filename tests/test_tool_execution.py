import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.storage.calls import CallStore
from app.storage.database import Database
from app.storage.models import BudgetCharge, StateChange
from app.storage.runs import RunStore
from app.tools.executor import ToolExecutor
from app.tools.models import ToolFailure, ToolInvocation, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition


class ValueParameters(ToolParameters):
    value: int


def invoke(tool_env, *, name="web_search", arguments='{"query":"AI"}', call_id="call-1", user=None):
    invocation = ToolInvocation(tool_call_id=call_id, name=name, arguments=arguments)
    return asyncio.run(
        tool_env.executor.execute(tool_env.run.id, user or tool_env.run.user_id, invocation)
    )


def custom(tool_env, handler, *, settings=None):
    tool_env.registry.register(ToolDefinition("sample", "测试执行器", ValueParameters, handler))
    if settings is not None:
        tool_env.executor = ToolExecutor(tool_env.registry, tool_env.calls, settings)


def test_stub_search_fetch_and_zero_results_have_uniform_feedback(tool_env) -> None:
    searched = invoke(tool_env)
    assert searched.ok
    assert searched.data["fixture"] is True
    assert searched.data["items"][0]["candidate_id"] == "fixture-model"
    fetched = invoke(
        tool_env, name="fetch_url", arguments='{"candidate_id":"fixture-model"}', call_id="fetch"
    )
    assert fetched.ok and "8B" in fetched.data["text"]
    empty = invoke(tool_env, arguments='{"query":"zero_results"}', call_id="empty")
    assert empty.ok and empty.data["items"] == []
    usage = tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage
    assert (usage.tool_calls, usage.searches, usage.fetches) == (3, 2, 1)
    for call_id in ("call-1", "fetch", "empty"):
        assert tool_env.calls.get_call(tool_env.run.id, "user-1", call_id).status == "completed"


def test_fixture_domain_language_and_limit_parameters(tool_env) -> None:
    excluded = invoke(tool_env, arguments='{"query":"AI","domains":["different.example"]}')
    assert excluded.data["items"] == []
    included = invoke(
        tool_env,
        arguments='{"query":"AI","domains":["example.com"],"language":"en","limit":1}',
        call_id="en",
    )
    assert len(included.data["items"]) == 1
    assert included.data["items"][0]["language"] == "en"


@pytest.mark.parametrize(
    "name,arguments,code,retryable",
    [
        ("web_search", '{"query":"simulate_failure"}', "SEARCH_UNAVAILABLE", True),
        ("fetch_url", '{"candidate_id":"unknown"}', "CANDIDATE_NOT_FOUND", False),
        ("unknown_tool", "{}", "UNKNOWN_TOOL", False),
    ],
)
def test_failures_are_saved_and_reused(tool_env, name, arguments, code, retryable) -> None:
    result = invoke(tool_env, name=name, arguments=arguments)
    assert result.error.code == code
    assert result.error.retryable is retryable
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").result == result
    before = tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage
    assert invoke(tool_env, name=name, arguments=arguments) == result
    assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage == before


@pytest.mark.parametrize(
    "arguments",
    [
        "not json",
        "[]",
        "null",
        "{}",
        '{"query":"AI","limit":"2"}',
        '{"query":"AI","limit":true}',
        '{"query":"AI","limit":11}',
        '{"query":"AI","language":"fr"}',
        '{"query":"AI","limit":NaN}',
        '{"query":"AI","query":"changed"}',
        '{"query":"AI","user_id":"other"}',
        '{"query":"AI","run_id":"other"}',
        '{"query":"AI","recipient":"other@example.com"}',
        '{"query":"AI","working_dir":"../other"}',
    ],
)
def test_invalid_arguments_are_feedback_not_execution(tool_env, arguments) -> None:
    result = invoke(tool_env, arguments=arguments)
    assert result.error.code == "INVALID_ARGUMENTS"
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").result == result
    usage = tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage
    assert usage.tool_calls == 1 and usage.searches == 0
    assert "other@example.com" not in result.model_dump_json()


def test_validation_feedback_is_specific_without_echoing_raw_inputs(tool_env) -> None:
    result = invoke(tool_env, arguments='{"query":123,"unknown":"fixture-deep-key"}')
    assert any(
        issue.field == "query" and issue.issue == "string_type" for issue in result.error.details
    )
    assert any(issue.field == "[unknown]" for issue in result.error.details)
    assert "fixture-deep-key" not in result.model_dump_json()


def test_wrong_user_cannot_prepare_or_execute_calls(tool_env) -> None:
    result = invoke(tool_env, user="other-user")
    assert result.error.code == "PERMISSION_DENIED"
    with tool_env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.tool_calls == 0


def test_existing_call_id_with_different_arguments_cannot_overwrite_result(tool_env) -> None:
    original = invoke(tool_env)
    result = invoke(tool_env, arguments='{"query":"changed"}')
    assert result.error.code == "CALL_ID_CONFLICT"
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").result == original


def test_same_arguments_with_new_call_id_are_executed_again(tool_env) -> None:
    invoke(tool_env)
    invoke(tool_env, call_id="new-call")
    assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.searches == 2


def test_success_is_reused_after_reopening_database(tool_env) -> None:
    original = invoke(tool_env)
    database = Database(tool_env.database.path)
    database.initialize()
    calls = CallStore(RunStore(database, tool_env.settings))
    executor = ToolExecutor(tool_env.registry, calls, tool_env.settings)
    result = asyncio.run(
        executor.execute(
            tool_env.run.id,
            "user-1",
            ToolInvocation(tool_call_id="call-1", name="web_search", arguments='{"query":"AI"}'),
        )
    )
    assert result == original
    assert calls.runs.get_run(tool_env.run.id, "user-1").budget_usage.tool_calls == 1


def test_executing_call_is_not_repeated(tool_env) -> None:
    invocation = ToolInvocation(
        tool_call_id="call-1", name="web_search", arguments='{"query":"AI"}'
    )
    tool_env.calls.prepare_calls(tool_env.run.id, "user-1", [invocation])
    tool_env.calls.start_call(tool_env.run.id, "user-1", "call-1", BudgetCharge(tool_calls=1))
    result = invoke(tool_env)
    assert result.error.code == "CALL_IN_PROGRESS"
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").status == "executing"
    assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.tool_calls == 1


def test_budget_exhaustion_is_saved_without_execution_or_extra_count(tool_env) -> None:
    tool_env.runs.consume_budget(tool_env.run.id, "user-1", BudgetCharge(tool_calls=50))
    result = invoke(tool_env)
    assert result.error.code == "BUDGET_EXCEEDED"
    record = tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1")
    assert record.status == "completed" and record.started_at is None
    usage = tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage
    assert usage.tool_calls == 50 and usage.searches == 0


@pytest.mark.parametrize("state", ["completed", "waiting_retry"])
def test_non_running_runs_return_feedback_without_executing(tool_env, state) -> None:
    change = StateChange(
        status=state,
        retry_at=datetime.now(UTC) + timedelta(seconds=5) if state == "waiting_retry" else None,
    )
    tool_env.runs.transition(tool_env.run.id, "user-1", change)
    result = invoke(tool_env)
    assert result.error.code == ("RUN_FINISHED" if state == "completed" else "RUN_NOT_RUNNING")
    assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.tool_calls == 0
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").result == result


def test_handler_observes_persisted_execution_and_bound_context(tool_env) -> None:
    async def handler(context, parameters):
        record = tool_env.calls.get_call(context.run_id, context.user_id, "call-1")
        assert record.status == "executing"
        assert tool_env.runs.get_run(context.run_id, context.user_id).budget_usage.tool_calls == 1
        assert context.recipient == "reader@example.com"
        assert context.working_dir == tool_env.settings.data_dir / "runs" / context.run_id
        assert not hasattr(context, "settings")
        return ToolResult.success({"value": parameters.value})

    custom(tool_env, handler)
    assert invoke(tool_env, name="sample", arguments='{"value":3}').data == {"value": 3}


@pytest.mark.parametrize("failure", ["generic", "permission", "controlled", "invalid_result"])
def test_handler_errors_are_safe_structured_feedback(tool_env, failure) -> None:
    async def handler(context, parameters):
        if failure == "permission":
            raise PermissionError("private path: fixture-deep-key")
        if failure == "controlled":
            raise ToolFailure("DEPENDENCY_FAILED", "fixture-deep-key", retryable=True)
        if failure == "invalid_result":
            return {"unexpected": "fixture-deep-key"}
        raise RuntimeError("password=fixture-deep-key")

    custom(tool_env, handler)
    result = invoke(tool_env, name="sample", arguments='{"value":1}')
    assert not result.ok
    assert (
        result.error.code
        == {
            "permission": "PERMISSION_DENIED",
            "controlled": "DEPENDENCY_FAILED",
            "invalid_result": "INTERNAL_TOOL_ERROR",
            "generic": "INTERNAL_TOOL_ERROR",
        }[failure]
    )
    assert "fixture-deep-key" not in result.model_dump_json()


def test_timeout_is_feedback_and_cancels_handler(tool_env) -> None:
    cancelled = []

    async def handler(context, parameters):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.append(True)

    settings = Settings(
        _env_file=None, data_dir=tool_env.settings.data_dir, request_timeout_seconds=0.01
    )
    custom(tool_env, handler, settings=settings)
    result = invoke(tool_env, name="sample", arguments='{"value":1}')
    assert result.error.code == "TOOL_TIMEOUT" and result.error.retryable
    assert cancelled == [True]


def test_cancellation_keeps_executing_record_for_recovery(tool_env) -> None:
    async def handler(context, parameters):
        raise asyncio.CancelledError

    custom(tool_env, handler)
    with pytest.raises(asyncio.CancelledError):
        invoke(tool_env, name="sample", arguments='{"value":1}')
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").status == "executing"


def test_large_results_become_bounded_feedback(tool_env) -> None:
    async def handler(context, parameters):
        return ToolResult.success({"text": "新闻" * 1000})

    settings = Settings(
        _env_file=None, data_dir=tool_env.settings.data_dir, max_tool_result_chars=256
    )
    custom(tool_env, handler, settings=settings)
    result = invoke(tool_env, name="sample", arguments='{"value":1}')
    assert result.error.code == "RESULT_TOO_LARGE"
    assert len(result.model_dump_json()) <= 256
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").result == result


def test_secrets_are_filtered_from_returned_and_saved_results(tool_env) -> None:
    async def handler(context, parameters):
        return ToolResult.success(
            {
                "text": "fixture-deep-key",
                "nested": [{"api_key": "unknown-provider-key"}],
                "fixture-deep-key": "key-is-also-redacted",
                "headers": "Authorization: Bearer unknown-key",
            }
        )

    custom(tool_env, handler)
    result = invoke(tool_env, name="sample", arguments='{"value":1}')
    encoded = result.model_dump_json()
    assert "fixture-deep-key" not in encoded
    assert "unknown-provider-key" not in encoded
    assert "unknown-key" not in encoded
    assert tool_env.calls.get_call(tool_env.run.id, "user-1", "call-1").result == result


def test_non_finite_result_is_rejected(tool_env) -> None:
    async def handler(context, parameters):
        return ToolResult.success({"number": float("nan")})

    custom(tool_env, handler)
    assert (
        invoke(tool_env, name="sample", arguments='{"value":1}').error.code == "INTERNAL_TOOL_ERROR"
    )


def test_argument_size_limit_is_structured_feedback(tool_env) -> None:
    settings = Settings(
        _env_file=None, data_dir=tool_env.settings.data_dir, max_model_input_chars=10
    )
    tool_env.executor = ToolExecutor(tool_env.registry, tool_env.calls, settings)
    assert invoke(tool_env).error.code == "INVALID_ARGUMENTS"


def test_bound_path_permission_failure_does_not_execute(tool_env, monkeypatch) -> None:
    original = Path.resolve
    target = tool_env.settings.data_dir / "runs" / tool_env.run.id

    def resolve(path, *args, **kwargs):
        if path == target:
            return tool_env.settings.data_dir.parent / "outside"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert invoke(tool_env).error.code == "PERMISSION_DENIED"
    assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.searches == 0
