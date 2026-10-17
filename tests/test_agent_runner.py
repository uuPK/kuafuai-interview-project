import asyncio

import pytest

from app.agent.protocol import AssistantMessage, ModelFailure, ModelRequest, ToolMessage
from app.agent.runner import AgentRunner, RecoveryRequiredError
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.models import BudgetCharge, RunRequest, StateChange
from app.storage.runs import BudgetExceededError, RunNotFoundError, RunStore
from app.tools.executor import ToolExecutor
from app.tools.models import ToolParameters, ToolResult
from app.tools.registry import ToolDefinition


def run(env, model, *, settings=None):
    return asyncio.run(env.runner(model, settings).run(env.run.id, env.run.user_id))


def submit(factory, call_id="submit", title="测试简报"):
    return factory(
        (call_id, "submit_brief", {"draft": {"title": title, "items": ["固定测试新闻"]}})
    )


def observation(request):
    feedback = next(m for m in reversed(request.messages) if isinstance(m, ToolMessage))
    assistant = next(m for m in reversed(request.messages) if isinstance(m, AssistantMessage))
    call = next(c for c in assistant.tool_calls if c.id == feedback.tool_call_id)
    return call.function.name, ToolResult.model_validate_json(feedback.content)


@pytest.mark.parametrize("query", ["AI", "zero_results"])
def test_model_changes_query_after_zero_results(agent_env, response_factory, query) -> None:
    env = agent_env

    def decide(request):
        name, result = observation(request)
        if name == "web_search":
            if not result.data["items"]:
                return response_factory(
                    (
                        "english",
                        "web_search",
                        {"query": "open source AI", "language": "en", "domains": ["example.com"]},
                    )
                )
            return response_factory(
                ("fetch", "fetch_url", {"candidate_id": result.data["items"][0]["candidate_id"]})
            )
        assert name == "fetch_url" and result.ok
        return submit(response_factory)

    model = ScriptedModel(
        [response_factory(("initial", "web_search", {"query": query})), decide, decide, decide]
    )
    completed = run(env, model)
    assert completed.status == "completed"
    assert completed.budget_usage.model_requests == (4 if query == "zero_results" else 3)
    responses = env.conversations.responses(env.run.id, "user-1")
    assert [c.id for response in responses for c in response.message.tool_calls] == (
        ["initial", "english", "fetch", "submit"]
        if query == "zero_results"
        else ["initial", "fetch", "submit"]
    )
    if query == "zero_results":
        assert '"language": "en"' in responses[1].message.tool_calls[0].function.arguments
    for request in model.requests:
        assert request.tool_choice == "auto"
        encoded = request.model_dump_json()
        assert "reader@example.com" not in encoded
        assert "fixture-deep-key" not in encoded
        assert env.run.user_id not in encoded


@pytest.mark.parametrize("candidate", ["fixture-model", "missing"])
def test_model_changes_source_after_fetch_failure(agent_env, response_factory, candidate) -> None:
    env = agent_env

    def decide(request):
        name, result = observation(request)
        if name == "fetch_url":
            if result.ok:
                return submit(response_factory)
            assert result.error.code == "CANDIDATE_NOT_FOUND"
            return response_factory(("alternative", "web_search", {"query": "alternative source"}))
        return response_factory(
            ("replacement", "fetch_url", {"candidate_id": result.data["items"][0]["candidate_id"]})
        )

    model = ScriptedModel(
        [
            response_factory(("first-fetch", "fetch_url", {"candidate_id": candidate})),
            decide,
            decide,
            decide,
        ]
    )
    completed = run(env, model)
    assert completed.status == "completed"
    assert completed.budget_usage.searches == (1 if candidate == "missing" else 0)
    assert len(model.requests) == (4 if candidate == "missing" else 2)


def test_batch_executes_exact_model_order_after_entire_response_is_saved(
    agent_env, response_factory
):
    env = agent_env
    order = []

    class Parameters(ToolParameters):
        value: int

    response = response_factory(
        ("two", "observe", {"value": 2}), ("one", "observe", {"value": 1}), raw={"full": "response"}
    )

    async def handler(context, parameters):
        assert env.conversations.responses(context.run_id, context.user_id)[0] == response
        for call_id in ("two", "one"):
            assert env.calls.get_call(context.run_id, context.user_id, call_id) is not None
        order.append(parameters.value)
        return ToolResult.success({"value": parameters.value})

    env.registry.register(ToolDefinition("observe", "观察执行顺序", Parameters, handler))
    model = ScriptedModel([response, submit(response_factory)])
    assert run(env, model).status == "completed"
    assert order == [2, 1]
    feedback = [m for m in model.requests[1].messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in feedback] == ["two", "one"]


@pytest.mark.parametrize("invalid", ["unknown", "bad_args"])
def test_parameter_and_unknown_tool_feedback_reach_next_decision(
    agent_env, response_factory, invalid
):
    first = response_factory(("invalid", "absent" if invalid == "unknown" else "web_search", {}))

    def corrected(request):
        _, result = observation(request)
        assert result.error.code == (
            "UNKNOWN_TOOL" if invalid == "unknown" else "INVALID_ARGUMENTS"
        )
        return submit(response_factory)

    assert run(agent_env, ScriptedModel([first, corrected])).status == "completed"


def test_successful_submission_skips_remaining_calls_but_pairs_every_id(
    agent_env, response_factory
):
    env = agent_env
    response = response_factory(
        ("accepted", "submit_brief", {"draft": {"title": "测试稿件"}}),
        ("late-search", "web_search", {"query": "must not run"}),
        ("late-submit", "submit_brief", {"draft": {"title": "must not replace"}}),
    )
    model = ScriptedModel([response])
    completed = run(env, model)
    assert completed.status == "completed"
    assert completed.budget_usage.tool_calls == 1 and completed.budget_usage.searches == 0
    assert len(model.requests) == 1
    messages = env.conversations.messages(env.run.id, "user-1")
    feedback = [m for m in messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in feedback] == ["accepted", "late-search", "late-submit"]
    assert [ToolResult.model_validate_json(m.content).error.code for m in feedback[1:]] == [
        "RUN_FINISHED"
    ] * 2
    assert env.calls.get_call(env.run.id, "user-1", "late-search").started_at is None
    assert (
        env.calls.get_call(env.run.id, "user-1", "accepted").result.data["draft"]["title"]
        == "测试稿件"
    )
    ModelRequest(messages=messages, tools=tuple(env.registry.schemas()))


def test_rejected_submission_is_feedback_and_model_can_revise(agent_env, response_factory) -> None:
    def revise(request):
        name, result = observation(request)
        assert name == "submit_brief" and result.error.code == "BRIEF_REJECTED"
        return submit(response_factory, call_id="revision", title="已修改测试稿")

    completed = run(
        agent_env, ScriptedModel([submit(response_factory, title="simulate_rejection"), revise])
    )
    assert completed.status == "completed" and completed.budget_usage.tool_calls == 2


@pytest.mark.parametrize("submit_after_reminder", [True, False])
def test_text_without_submission_receives_one_reminder(
    agent_env, response_factory, submit_after_reminder
):
    def after_reminder(request):
        assert request.messages[-1].role == "user"
        assert "唯一" in request.messages[-1].content
        assert request.wire_messages()[2]["reasoning_content"] == "historical reasoning"
        return (
            submit(response_factory)
            if submit_after_reminder
            else response_factory(content="仍只有普通文本")
        )

    model = ScriptedModel(
        [response_factory(content="完成了", reasoning="historical reasoning"), after_reminder]
    )
    result = run(agent_env, model)
    assert result.status == ("completed" if submit_after_reminder else "failed")
    assert result.stop_reason == (None if submit_after_reminder else "missing_submission")
    assert len(model.requests) == 2
    events = agent_env.runs.list_events(result.id, result.user_id)
    assert sum(event.kind == "submission_reminder" for event in events) == 1
    assert "historical reasoning" not in str(events)


def test_saved_pending_response_is_resumed_before_any_model_request(agent_env, response_factory):
    env = agent_env
    env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(model_requests=1))
    response = response_factory(
        ("saved-search", "web_search", {"query": "AI"}), reasoning="saved reasoning"
    )
    env.conversations.save_response(env.run.id, "user-1", response)
    database = Database(env.database.path)
    database.initialize()
    runs = RunStore(database, env.settings)
    calls = CallStore(runs)
    store = ConversationStore(calls)
    executor = ToolExecutor(env.registry, calls, env.settings)

    def after_search(request):
        _, result = observation(request)
        assert result.ok
        assert calls.get_call(env.run.id, "user-1", "saved-search").status == "completed"
        return submit(response_factory)

    model = ScriptedModel([after_search])
    completed = asyncio.run(
        AgentRunner(model, executor, store, env.settings).run(env.run.id, "user-1")
    )
    assert completed.status == "completed"
    assert completed.budget_usage.model_requests == 2 and completed.budget_usage.tool_calls == 2
    assert (completed.window_start, completed.window_end) == (
        env.run.window_start,
        env.run.window_end,
    )
    assert store.responses(env.run.id, "user-1")[0] == response


def test_saved_result_is_paired_without_reexecution_or_extra_budget(agent_env, response_factory):
    env = agent_env
    response = response_factory(("saved", "web_search", {"query": "AI"}))
    env.conversations.save_response(env.run.id, "user-1", response)
    asyncio.run(
        env.executor.execute(env.run.id, "user-1", response.message.tool_calls[0].invocation())
    )
    before = env.runs.get_run(env.run.id, "user-1").budget_usage
    completed = run(env, ScriptedModel([submit(response_factory)]))
    assert completed.budget_usage.searches == before.searches == 1
    assert completed.budget_usage.tool_calls == before.tool_calls + 1


def test_uncertain_executing_call_requires_recovery_without_new_decisions(
    agent_env, response_factory
):
    env = agent_env
    env.conversations.save_response(
        env.run.id, "user-1", response_factory(("uncertain", "web_search", {"query": "AI"}))
    )
    env.calls.start_call(env.run.id, "user-1", "uncertain", BudgetCharge(tool_calls=1))
    model = ScriptedModel([submit(response_factory)])
    with pytest.raises(RecoveryRequiredError):
        run(env, model)
    assert model.requests == []
    assert env.runs.get_run(env.run.id, "user-1").status == "running"
    assert env.calls.get_call(env.run.id, "user-1", "uncertain").status == "executing"
    assert len(env.conversations.messages(env.run.id, "user-1")) == 3


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_terminal_run_pairs_unstarted_calls_without_execution(
    agent_env, response_factory, terminal
):
    env = agent_env
    env.conversations.save_response(
        env.run.id, "user-1", response_factory(("a", "web_search", {"query": "AI"}))
    )
    env.runs.transition(
        env.run.id,
        "user-1",
        StateChange(status=terminal, stop_reason="test_stop" if terminal == "failed" else None),
    )
    model = ScriptedModel([])
    assert run(env, model).status == terminal
    assert model.requests == []
    assert env.calls.get_call(env.run.id, "user-1", "a").result.error.code == "RUN_FINISHED"
    assert env.runs.get_run(env.run.id, "user-1").budget_usage.tool_calls == 0


@pytest.mark.parametrize("state", ["queued", "waiting_retry"])
def test_runner_starts_queued_run_and_leaves_waiting_run_to_worker(
    agent_env, response_factory, state
):
    env = agent_env
    if state == "queued":
        from app.agent.prompts import initial_messages

        env.run = env.runs.create_run(
            RunRequest(
                user_id="user-1", subscription_id="new-sub", subscription=env.run.subscription
            )
        )
        model = ScriptedModel([submit(response_factory)])
        assert run(env, model).status == "completed"
        assert env.conversations.messages(env.run.id, "user-1")[:2] == initial_messages(
            env.runs.get_run(env.run.id, "user-1").model_copy(
                update={"budget_usage": env.run.budget_usage}
            )
        )
    else:
        from datetime import UTC, datetime, timedelta

        env.runs.transition(
            env.run.id,
            "user-1",
            StateChange(status="waiting_retry", retry_at=datetime.now(UTC) + timedelta(seconds=10)),
        )
        model = ScriptedModel([])
        assert run(env, model).status == "waiting_retry"
        assert model.requests == []


def test_response_save_failure_does_not_execute_any_tool(agent_env, response_factory, monkeypatch):
    env = agent_env
    append = env.runs._append_event

    def fail(connection, run_id, kind, at, data):
        if kind == "model_response_saved":
            raise RuntimeError("simulated database failure")
        append(connection, run_id, kind, at, data)

    monkeypatch.setattr(env.runs, "_append_event", fail)
    with pytest.raises(RuntimeError):
        run(env, ScriptedModel([submit(response_factory)]))
    assert env.runs.get_run(env.run.id, "user-1").budget_usage.tool_calls == 0
    assert env.conversations.responses(env.run.id, "user-1") == ()


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("invalid", "model_protocol_error"),
        ("controlled", "model_dependency_error"),
        ("generic", "model_internal_error"),
        ("timeout", "model_timeout"),
    ],
)
def test_model_failures_have_safe_persisted_stop_reasons(agent_env, failure, reason):
    class Model:
        attempts = 0

        async def complete(self, request):
            self.attempts += 1
            assert (
                agent_env.runs.get_run(agent_env.run.id, "user-1").budget_usage.model_requests
                == self.attempts
            )
            if failure == "invalid":
                return {"unexpected": "fixture-deep-key"}
            if failure == "controlled":
                raise ModelFailure("UNAVAILABLE", retryable=True)
            if failure == "timeout":
                await asyncio.sleep(1)
            raise RuntimeError("fixture-deep-key")

    settings = Settings(
        _env_file=None, data_dir=agent_env.settings.data_dir, request_timeout_seconds=0.01
    )
    failed = run(agent_env, Model(), settings=settings)
    assert failed.status == "failed" and failed.stop_reason == reason
    assert "fixture-deep-key" not in str(agent_env.runs.list_events(failed.id, failed.user_id))


def test_cancellation_does_not_fabricate_a_model_response(agent_env):
    class Model:
        async def complete(self, request):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(agent_env, Model())
    assert agent_env.conversations.responses(agent_env.run.id, "user-1") == ()
    assert agent_env.runs.get_run(agent_env.run.id, "user-1").budget_usage.model_requests == 1


def test_truncated_response_is_saved_but_its_calls_are_not_executed(agent_env, response_factory):
    response = response_factory(("a", "web_search", {"query": "AI"}), finish_reason="length")
    failed = run(agent_env, ScriptedModel([response]))
    assert failed.stop_reason == "model_incomplete_response"
    assert agent_env.conversations.responses(failed.id, failed.user_id) == (response,)
    assert (
        agent_env.calls.get_call(failed.id, failed.user_id, "a").result.error.code == "RUN_FINISHED"
    )
    assert failed.budget_usage.tool_calls == 0


def test_reusing_a_previous_call_id_stops_protocol_without_overwrite(agent_env, response_factory):
    response = response_factory(("same", "web_search", {"query": "AI"}))
    model = ScriptedModel([response, response])
    failed = run(agent_env, model)
    assert failed.stop_reason == "model_protocol_error"
    assert len(agent_env.conversations.responses(failed.id, failed.user_id)) == 1
    assert failed.budget_usage.tool_calls == 1


def test_input_limit_stops_without_truncating_history_or_calling_model(agent_env):
    settings = Settings(
        _env_file=None, data_dir=agent_env.settings.data_dir, max_model_input_chars=10
    )
    model = ScriptedModel([])
    assert run(agent_env, model, settings=settings).stop_reason == "model_input_too_large"
    assert model.requests == []
    assert len(agent_env.conversations.messages(agent_env.run.id, "user-1")) == 2


def test_runner_warns_about_input_capacity_before_submission(agent_env, response_factory):
    env = agent_env
    request = ModelRequest(
        messages=env.conversations.messages(env.run.id, "user-1"),
        tools=tuple(env.registry.schemas()),
    )
    settings = env.settings.model_copy(
        update={"max_model_input_chars": len(request.model_dump_json()) + 1000}
    )
    model = ScriptedModel([submit(response_factory)])
    assert run(env, model, settings=settings).status == "completed"
    assert any(
        "保留一次修订" in (getattr(m, "content", None) or "") for m in model.requests[0].messages
    )


def test_model_request_budget_is_checked_before_request(agent_env, response_factory):
    env = agent_env
    env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(model_requests=15))
    model = ScriptedModel([submit(response_factory)])
    assert run(env, model).stop_reason == "budget_model_requests"
    assert model.requests == []


def test_tool_budget_stop_pairs_rest_of_batch(agent_env, response_factory):
    env = agent_env
    env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(tool_calls=50))
    response = response_factory(
        ("a", "web_search", {"query": "AI"}), ("b", "web_search", {"query": "later"})
    )
    failed = run(env, ScriptedModel([response]))
    assert failed.stop_reason == "tool_budget_exceeded"
    assert env.calls.get_call(env.run.id, "user-1", "a").result.error.code == "BUDGET_EXCEEDED"
    assert env.calls.get_call(env.run.id, "user-1", "b").result.error.code == "RUN_FINISHED"
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()


def test_deadline_at_submission_commit_saves_failure_and_stops(
    agent_env, response_factory, monkeypatch
):
    env = agent_env
    transition = env.runs._transition

    def deadline(connection, run, change, at):
        if change.status == "completed":
            raise BudgetExceededError("deadline")
        return transition(connection, run, change, at)

    monkeypatch.setattr(env.runs, "_transition", deadline)
    failed = run(env, ScriptedModel([submit(response_factory)]))
    assert failed.stop_reason == "budget_deadline"
    record = env.calls.get_call(env.run.id, "user-1", "submit")
    assert record.status == "completed" and record.result.error.code == "BUDGET_EXCEEDED"
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()


def test_runner_checks_owner_and_consistent_stores(agent_env):
    env = agent_env
    with pytest.raises(RunNotFoundError):
        asyncio.run(env.runner(ScriptedModel([])).run(env.run.id, "other"))
    with pytest.raises(ValueError, match="share"):
        AgentRunner(
            ScriptedModel([]), env.executor, ConversationStore(CallStore(env.runs)), env.settings
        )


def test_execution_claim_race_is_not_recorded_as_final_feedback(
    agent_env, response_factory, monkeypatch
):
    env = agent_env
    env.conversations.save_response(
        env.run.id, "user-1", response_factory(("a", "web_search", {"query": "AI"}))
    )

    async def claimed_elsewhere(run_id, user_id, invocation, **kwargs):
        env.calls.start_call(run_id, user_id, invocation.tool_call_id, BudgetCharge(tool_calls=1))
        return ToolResult.failure("CALL_IN_PROGRESS", "已由另一执行者启动。", retryable=True)

    monkeypatch.setattr(env.executor, "execute", claimed_elsewhere)
    model = ScriptedModel([])
    with pytest.raises(RecoveryRequiredError):
        run(env, model)
    assert model.requests == []
    assert env.calls.get_call(env.run.id, "user-1", "a").status == "executing"
    assert env.conversations.messages(env.run.id, "user-1")[-1].role == "assistant"
