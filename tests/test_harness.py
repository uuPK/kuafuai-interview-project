import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.protocol import ModelFailure, ModelRequest, ToolMessage
from app.agent.runner import AgentRunner, RecoveryRequiredError
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.harness.failures import FailureInfo, RequestInProgressError, RetryNotDueError
from app.harness.guardrails import action_hash
from app.harness.retry import retry_after_seconds, retry_delay
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.models import BudgetCharge, RunRequest, StateChange, SubscriptionSnapshot
from app.storage.runs import BudgetExceededError, InvalidRunStateError, RunNotFoundError, RunStore
from app.tools.executor import ToolExecutor
from app.tools.models import ToolFailure, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition
from app.tools.stubs import build_stub_registry, register_submission_fixture
from tests.test_agent_runner import observation, submit


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 11, tzinfo=UTC)
        self.waits = []

    def now(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.advance(seconds)


@pytest.fixture
def harness(tmp_path):
    def make(**overrides):
        settings = Settings(
            _env_file=None, data_dir=tmp_path, deepseek_api_key="fixture-deep-key", **overrides
        )
        clock = Clock()
        database = Database(settings.database_path)
        database.initialize()
        runs = RunStore(database, settings, clock=clock.now)
        run = runs.create_run(
            RunRequest(
                user_id="user-1",
                subscription_id="sub-1",
                subscription=SubscriptionSnapshot(
                    nickname="研究员", email="reader@example.com", topics=("开源模型",)
                ),
            )
        )
        calls = CallStore(runs)
        registry = build_stub_registry()
        register_submission_fixture(registry)
        conversations = ConversationStore(calls)
        executor = ToolExecutor(registry, calls, settings, sleep=clock.sleep)
        return SimpleNamespace(
            settings=settings,
            clock=clock,
            database=database,
            runs=runs,
            run=run,
            calls=calls,
            registry=registry,
            conversations=conversations,
            executor=executor,
            requests=conversations.requests,
        )

    return make


def drive(env, model):
    return asyncio.run(
        AgentRunner(model, env.executor, env.conversations, env.settings).run(env.run.id, "user-1")
    )


def reopen(env):
    database = Database(env.database.path)
    database.initialize()
    env.runs = RunStore(database, env.settings, clock=env.clock.now)
    env.calls = CallStore(env.runs)
    env.conversations = ConversationStore(env.calls)
    env.requests = env.conversations.requests
    env.executor = ToolExecutor(env.registry, env.calls, env.settings, sleep=env.clock.sleep)


def unavailable(request):
    raise ModelFailure("RATE_LIMITED", retryable=True)


def notice_count(env, purpose):
    with env.database.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM run_messages WHERE run_id = ? AND purpose = ?",
            (env.run.id, purpose),
        ).fetchone()[0]


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, None),
        ("", None),
        ("5", 5),
        (" 0 ", 0),
        ("-1", None),
        ("1.5", None),
        ("NaN", None),
        ("五", None),
        ("bad date", None),
        ("Sun, 11 Oct 2026 00:00:09 GMT", 9),
        ("Sat, 10 Oct 2026 00:00:00 GMT", 0),
    ],
)
def test_retry_after(header, expected):
    assert retry_after_seconds(header, Clock().now()) == expected


def test_backoff_never_shortens_server_wait():
    assert retry_delay(2, 2, "1", Clock().now()) == 4
    assert retry_delay(2, 2, "8", Clock().now()) == 8
    assert retry_delay(2, 2, "invalid", Clock().now()) == 4


@pytest.mark.parametrize("category", ["authentication", "quota", "configuration", "protocol"])
def test_fatal_category_overrides_retryable(category):
    assert not FailureInfo(code="FATAL", category=category, retryable=True).retryable
    assert not ModelFailure("FATAL", category=category, retryable=True).retryable
    assert not ToolFailure(
        "FATAL", "服务错误", category=category, retryable=True
    ).result.error.retryable


@pytest.mark.parametrize("value", [-1, 31, float("nan"), float("inf")])
def test_invalid_inline_wait_setting(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retry_inline_max_seconds=value)


def test_model_retry_counts_attempts_and_freezes_input(harness, response_factory):
    env = harness()
    model = ScriptedModel([unavailable, unavailable, submit(response_factory)])
    completed = drive(env, model)
    assert completed.status == "completed" and completed.budget_usage.model_requests == 3
    assert completed.budget_usage.tool_calls == 1
    assert env.clock.waits == [1, 2]
    assert model.requests[0] == model.requests[1] == model.requests[2]
    with env.database.connection() as connection:
        requests = connection.execute(
            "SELECT * FROM external_requests WHERE kind = 'model'"
        ).fetchall()
        assert len(requests) == 1 and requests[0]["attempts"] == 3
        assert requests[0]["status"] == "completed"
        assert (
            connection.execute("SELECT request_id FROM model_responses").fetchone()[0]
            == requests[0]["id"]
        )


def test_exhausted_model_retry_stops_after_three(harness):
    env = harness()
    model = ScriptedModel([unavailable] * 4)
    failed = drive(env, model)
    assert failed.stop_reason == "model_dependency_error"
    assert failed.budget_usage.model_requests == len(model.requests) == 3
    assert env.clock.waits == [1, 2]
    assert env.requests.active_model(env.run.id, "user-1") is None


@pytest.mark.parametrize("category", ["authentication", "quota", "configuration", "protocol"])
def test_model_fatal_dependency_stops_without_retry(harness, category):
    env = harness()

    def fail(request):
        raise ModelFailure("FATAL", category=category, retryable=True)

    model = ScriptedModel([fail])
    assert drive(env, model).stop_reason == f"model_dependency_{category}"
    assert len(model.requests) == 1 and env.clock.waits == []


@pytest.mark.parametrize("header", ["8", "Sun, 11 Oct 2026 00:00:08 GMT"])
def test_long_model_wait_returns_and_reopens_same_request(harness, response_factory, header):
    env = harness()

    def limited(request):
        raise ModelFailure("RATE_LIMITED", retryable=True, retry_after=header)

    first = ScriptedModel([limited])
    waiting = drive(env, first)
    assert waiting.status == "waiting_retry" and waiting.retry_at == env.clock.now() + timedelta(
        seconds=8
    )
    assert waiting.budget_usage.model_requests == 1 and env.clock.waits == []
    saved = env.requests.active_model(env.run.id, "user-1")
    assert saved.attempts == 1 and saved.failure.retry_after is None
    assert all(
        "retry_after" not in event.data for event in env.runs.list_events(env.run.id, "user-1")
    )
    reopen(env)
    assert drive(env, ScriptedModel([])).status == "waiting_retry"
    env.clock.advance(8)
    second = ScriptedModel([submit(response_factory)])
    completed = drive(env, second)
    assert completed.status == "completed" and completed.budget_usage.model_requests == 2
    assert second.requests == first.requests
    assert (
        completed.started_at,
        completed.deadline_at,
        completed.window_start,
        completed.window_end,
    ) == (
        waiting.started_at,
        waiting.deadline_at,
        waiting.window_start,
        waiting.window_end,
    )
    assert env.requests.get(saved.id, "user-1").attempts == 2


@pytest.mark.parametrize("header", ["600", "9" * 200])
def test_retry_after_beyond_deadline_does_not_sleep_or_call_again(harness, header):
    env = harness()

    def limited(request):
        raise ModelFailure("LIMITED", retryable=True, retry_after=header)

    model = ScriptedModel([limited])
    assert drive(env, model).stop_reason == "budget_deadline"
    assert len(model.requests) == 1 and env.clock.waits == []


def test_retry_resume_after_deadline_closes_model_request(harness):
    env = harness()

    def limited(request):
        raise ModelFailure("LIMITED", retryable=True, retry_after="8")

    drive(env, ScriptedModel([limited]))
    env.clock.advance(600)
    reopen(env)
    assert drive(env, ScriptedModel([])).stop_reason == "budget_deadline"
    assert env.requests.active_model(env.run.id, "user-1") is None


def test_final_model_decision_is_reserved_after_failure(harness, response_factory):
    env = harness(max_model_requests=2)

    def last_decision(request):
        assert "最后一次决策" in "".join(m.content or "" for m in request.messages)
        return submit(response_factory)

    model = ScriptedModel([unavailable, last_decision])
    completed = drive(env, model)
    assert completed.status == "completed" and completed.budget_usage.model_requests == 2
    assert env.clock.waits == [1] and notice_count(env, "retry_notice") == 1
    assert model.requests[0] != model.requests[1]


def test_last_tool_slot_and_final_decision_accept_only_submission(harness, response_factory):
    env = harness(max_tool_calls=2, max_model_requests=2)
    first = response_factory(("search", "web_search", {"query": "AI"}))

    def final(request):
        assert any("预算接近" in (message.content or "") for message in request.messages)
        return response_factory(
            ("blocked", "web_search", {"query": "extra"}),
            ("submit", "submit_brief", {"draft": {"title": "已有材料"}}),
        )

    completed = drive(env, ScriptedModel([first, final]))
    assert completed.status == "completed"
    assert completed.budget_usage.model_requests == 2 and completed.budget_usage.tool_calls == 2
    blocked = env.calls.get_call(env.run.id, "user-1", "blocked")
    assert blocked.started_at is None and blocked.result.error.code == "SUBMISSION_REQUIRED"
    assert notice_count(env, "budget_warning") == 1
    ModelRequest(
        messages=env.conversations.messages(env.run.id, "user-1"),
        tools=tuple(env.registry.schemas()),
    )


def test_tool_retry_is_one_logical_call_and_search(harness, response_factory):
    env = harness()
    attempts = []

    class Parameters(ToolParameters):
        query: str

    async def handler(context, parameters):
        run = env.runs.get_run(context.run_id, context.user_id)
        attempts.append(parameters.query)
        assert run.budget_usage.searches == run.budget_usage.tool_calls == 1
        if len(attempts) < 3:
            raise ToolFailure("TEMPORARY", "fixture-deep-key", retryable=True, retry_after="2")
        return ToolResult.success({"items": ["真实调用替身"]})

    env.registry.register(
        ToolDefinition(
            "retry_search",
            "重试测试",
            Parameters,
            handler,
            budget_dimension="searches",
            retry_safe=True,
        )
    )
    model = ScriptedModel(
        [
            response_factory(("search", "retry_search", {"query": "AI"})),
            submit(response_factory),
        ]
    )
    completed = drive(env, model)
    assert completed.status == "completed"
    assert attempts == ["AI"] * 3 and env.clock.waits == [2, 2]
    assert completed.budget_usage.searches == 1 and completed.budget_usage.tool_calls == 2
    request = env.requests.for_tool(env.run.id, "user-1", "search")
    assert request.attempts == 3 and "fixture-deep-key" not in request.feedback.model_dump_json()
    assert "retry_safe" not in str(env.registry.schemas())


@pytest.mark.parametrize("category", ["authentication", "quota", "configuration", "protocol"])
def test_tool_fatal_failure_stops_and_pairs_remaining_batch(harness, response_factory, category):
    env = harness()
    attempts = []

    async def handler(context, parameters):
        attempts.append(True)
        raise ToolFailure(
            "FATAL", "Authorization: fixture-deep-key", category=category, retryable=True
        )

    env.registry.register(
        ToolDefinition("fatal", "测试服务", ToolParameters, handler, retry_safe=True)
    )
    model = ScriptedModel(
        [
            response_factory(
                ("fatal", "fatal", {}),
                ("skip", "web_search", {"query": "AI"}),
            )
        ]
    )
    failed = drive(env, model)
    assert failed.stop_reason == f"tool_dependency_{category}" and attempts == [True]
    assert len(model.requests) == 1 and env.clock.waits == []
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()
    assert env.calls.get_call(env.run.id, "user-1", "skip").result.error.code == "RUN_FINISHED"
    assert (
        "fixture-deep-key"
        not in env.requests.for_tool(env.run.id, "user-1", "fatal").feedback.model_dump_json()
    )


@pytest.mark.parametrize("safe,attempts", [(False, 1), (True, 3)])
def test_tool_retry_requires_trusted_safe_flag(harness, response_factory, safe, attempts):
    env = harness()
    observed = []

    async def handler(context, parameters):
        observed.append(True)
        return ToolResult.failure("TEMPORARY", "临时错误", retryable=True)

    env.registry.register(
        ToolDefinition("unstable", "测试服务", ToolParameters, handler, retry_safe=safe)
    )
    model = ScriptedModel([response_factory(("tool", "unstable", {})), submit(response_factory)])
    assert drive(env, model).status == "completed"
    assert len(observed) == attempts
    assert env.requests.for_tool(env.run.id, "user-1", "tool").attempts == attempts
    assert observation(model.requests[1])[1].error.code == "TEMPORARY"


def test_long_tool_wait_reopens_without_duplicate_count_or_partial_feedback(
    harness, response_factory
):
    env = harness()
    attempts = []

    async def handler(context, parameters):
        attempts.append(True)
        if len(attempts) == 1:
            raise ToolFailure("LIMITED", "稍后再试", retryable=True, retry_after="8")
        return ToolResult.success({"recovered": True})

    env.registry.register(
        ToolDefinition("waiting", "测试服务", ToolParameters, handler, retry_safe=True)
    )
    first = ScriptedModel(
        [
            response_factory(
                ("tool", "waiting", {}),
                ("search", "web_search", {"query": "AI"}),
            )
        ]
    )
    waiting = drive(env, first)
    assert waiting.status == "waiting_retry" and waiting.budget_usage.tool_calls == 1
    assert env.conversations.messages(env.run.id, "user-1")[-1].role == "assistant"
    assert env.calls.get_call(env.run.id, "user-1", "search").status == "pending"
    # The runner has returned, so another run can finish while this one waits.
    other = env.runs.create_run(
        RunRequest(user_id="user-1", subscription_id="other", subscription=waiting.subscription)
    )
    assert (
        asyncio.run(
            AgentRunner(
                ScriptedModel([submit(response_factory)]),
                env.executor,
                env.conversations,
                env.settings,
            ).run(other.id, "user-1")
        ).status
        == "completed"
    )
    env.clock.advance(8)
    reopen(env)
    model = ScriptedModel([submit(response_factory)])
    completed = drive(env, model)
    assert completed.status == "completed" and attempts == [True, True]
    assert completed.budget_usage.tool_calls == 3 and completed.budget_usage.searches == 1
    assert completed.budget_usage.model_requests == 2
    feedback = [m.tool_call_id for m in model.requests[0].messages if isinstance(m, ToolMessage)]
    assert feedback == ["tool", "search"]
    assert completed.deadline_at == waiting.deadline_at


def test_tool_wait_expiring_pairs_batch_without_execution(harness, response_factory):
    env = harness()

    async def handler(context, parameters):
        raise ToolFailure("LIMITED", "稍后再试", retryable=True, retry_after="8")

    env.registry.register(
        ToolDefinition("waiting", "测试服务", ToolParameters, handler, retry_safe=True)
    )
    drive(env, ScriptedModel([response_factory(("tool", "waiting", {}))]))
    env.clock.advance(600)
    failed = drive(env, ScriptedModel([]))
    assert failed.stop_reason == "budget_deadline" and failed.budget_usage.tool_calls == 1
    assert env.calls.get_call(env.run.id, "user-1", "tool").result.error.code == "BUDGET_EXCEEDED"
    assert env.requests.for_tool(env.run.id, "user-1", "tool").status == "completed"
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()


@pytest.mark.parametrize(
    "name,args",
    [
        ("web_search", {"query": "zero_results"}),
        ("fetch_url", {"candidate_id": "missing"}),
        ("absent", {}),
    ],
)
def test_three_repeats_warn_then_fourth_stops(harness, response_factory, name, args):
    env = harness()
    first = [response_factory((f"call-{n}", name, args)) for n in range(3)]

    def repeat(request):
        assert "连续三次" in request.messages[-1].content
        return response_factory(("fourth", name, args), ("skip", "web_search", {"query": "AI"}))

    model = ScriptedModel([*first, repeat])
    failed = drive(env, model)
    assert failed.stop_reason == "repeated_failed_action"
    assert failed.budget_usage.tool_calls == 3 and len(model.requests) == 4
    fourth = env.calls.get_call(env.run.id, "user-1", "fourth")
    assert fourth.started_at is None and fourth.result.error.code == "REPEATED_ACTION_STOPPED"
    assert env.calls.get_call(env.run.id, "user-1", "skip").result.error.code == "RUN_FINISHED"
    assert notice_count(env, "repeat_warning") == 1
    ModelRequest(
        messages=env.conversations.messages(env.run.id, "user-1"),
        tools=tuple(env.registry.schemas()),
    )


def test_batch_repetition_is_blocked_until_model_receives_warning(harness, response_factory):
    env = harness()
    batch = response_factory(
        *[(f"call-{n}", "web_search", {"query": "zero_results"}) for n in range(4)]
    )

    def adjust(request):
        assert "连续三次" in request.messages[-1].content
        assert observation(request)[1].error.code == "ACTION_CHANGE_REQUIRED"
        return submit(response_factory)

    completed = drive(env, ScriptedModel([batch, adjust]))
    assert completed.status == "completed" and completed.budget_usage.searches == 3
    assert env.calls.get_call(env.run.id, "user-1", "call-3").started_at is None
    assert notice_count(env, "repeat_warning") == 1


def test_changed_action_and_success_reset_failure_streak(harness, response_factory):
    env = harness()
    steps = [
        response_factory((f"a-{n}", "web_search", {"query": "zero_results"})) for n in range(2)
    ]
    steps += [response_factory(("success", "web_search", {"query": "AI"}))]
    steps += [
        response_factory((f"b-{n}", "web_search", {"query": "zero_results"})) for n in range(2)
    ]
    steps += [
        response_factory(("changed", "fetch_url", {"candidate_id": "missing"})),
        submit(response_factory),
    ]
    assert drive(env, ScriptedModel(steps)).status == "completed"
    assert notice_count(env, "repeat_warning") == 0


def test_repeat_notice_survives_reopen_and_new_arguments_are_allowed(harness, response_factory):
    env = harness()
    run = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    from app.agent.prompts import initial_messages

    env.conversations.initialize(run.id, "user-1", initial_messages(run))
    for n in range(3):
        response = response_factory((f"a-{n}", "web_search", {"query": "zero_results"}))
        env.conversations.save_response(run.id, "user-1", response)
        asyncio.run(
            env.executor.execute(
                run.id,
                "user-1",
                response.message.tool_calls[0].invocation(),
                on_finished=AgentRunner(
                    ScriptedModel([]), env.executor, env.conversations, env.settings
                )._feedback("user-1"),
            )
        )
    env.conversations.flush_notices(run.id, "user-1", 30)
    reopen(env)
    steps = [response_factory(("changed", "web_search", {"query": "AI"})), submit(response_factory)]
    assert drive(env, ScriptedModel(steps)).status == "completed"
    assert notice_count(env, "repeat_warning") == 1


def test_action_identity_ignores_json_order_and_spacing():
    assert action_hash("search", '{"q":"AI", "limit":1}') == action_hash(
        "search", '{"limit": 1,"q":"AI"}'
    )
    assert action_hash("search", "bad json") == action_hash("search", "bad json")
    assert action_hash("search", "NaN") == action_hash("search", "NaN")
    assert action_hash("search", "{}") != action_hash("fetch", "{}")
    assert action_hash("search", '{"q":"AI", "q":"AI"}') != action_hash("search", '{"q":"AI"}')


def test_unknown_inflight_model_attempt_requires_recovery(harness):
    env = harness()

    class Cancelled:
        async def complete(self, request):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        drive(env, Cancelled())
    reopen(env)
    with pytest.raises(RecoveryRequiredError):
        drive(env, ScriptedModel([]))
    assert env.runs.get_run(env.run.id, "user-1").budget_usage.model_requests == 1


def test_retry_await_uses_no_open_database_transaction(harness, response_factory):
    env = harness()

    async def sleep(seconds):
        # A concurrent writer would fail here if the runner held a write transaction.
        with env.database.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS concurrent_writer (id INTEGER)")
        await env.clock.sleep(seconds)

    env.executor.sleep = sleep
    assert drive(env, ScriptedModel([unavailable, submit(response_factory)])).status == "completed"


def test_request_storage_owner_attempt_claim_and_retry_due(harness):
    env = harness()
    env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    request = env.requests.prepare(env.run.id, "user-1", "model", "{}", env.settings)
    for operation in (
        lambda: env.requests.get(request.id, "other"),
        lambda: env.requests.start(request.id, "other"),
        lambda: env.requests.active_model(env.run.id, "other"),
        lambda: env.requests.for_tool(env.run.id, "other", "missing"),
        lambda: env.requests.prepare(env.run.id, "other", "model", "{}", env.settings),
        lambda: env.requests.abandon(request.id, "other"),
        lambda: env.requests.resume_due_run(env.run.id, "other"),
    ):
        with pytest.raises(RunNotFoundError):
            operation()
    with pytest.raises(ValueError, match="payload"):
        env.requests.prepare(env.run.id, "user-1", "model", "changed", env.settings)
    env.requests.start(request.id, "user-1")
    with pytest.raises(RequestInProgressError):
        env.requests.start(request.id, "user-1")
    waiting = env.requests.fail_attempt(
        request.id,
        "user-1",
        FailureInfo(code="TEMPORARY", category="transient", retryable=True),
        env.settings,
    )
    assert waiting.status == "waiting"
    with pytest.raises(RetryNotDueError):
        env.requests.start(request.id, "user-1")
    with pytest.raises(RequestInProgressError):
        env.requests.fail_attempt(request.id, "user-1", FailureInfo(code="TEMPORARY"), env.settings)
    with pytest.raises(ValueError):
        env.requests.complete_tool(request.id, "user-1", ToolResult.success())


def test_request_start_rolls_back_attempt_and_budget_together(harness, monkeypatch):
    env = harness()
    env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    request = env.requests.prepare(env.run.id, "user-1", "model", "{}", env.settings)
    original = env.runs._append_event

    def broken(connection, run_id, kind, at, data):
        if kind == "request_attempt_started":
            raise RuntimeError("transaction interrupted")
        original(connection, run_id, kind, at, data)

    monkeypatch.setattr(env.runs, "_append_event", broken)
    with pytest.raises(RuntimeError):
        env.requests.start(request.id, "user-1")
    assert env.requests.get(request.id, "user-1").attempts == 0
    assert env.runs.get_run(env.run.id, "user-1").budget_usage.model_requests == 0


def test_model_response_transaction_rolls_back_request_completion(
    harness, response_factory, monkeypatch
):
    env = harness()
    original = env.runs._append_event

    def broken(connection, run_id, kind, at, data):
        if kind == "model_response_saved":
            raise RuntimeError("transaction interrupted")
        original(connection, run_id, kind, at, data)

    monkeypatch.setattr(env.runs, "_append_event", broken)
    with pytest.raises(RuntimeError):
        drive(env, ScriptedModel([submit(response_factory)]))
    assert env.requests.active_model(env.run.id, "user-1").status == "executing"
    assert env.conversations.responses(env.run.id, "user-1") == ()
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()


def test_limits_checked_before_physical_retry_and_settings_do_not_reset_snapshot(harness):
    env = harness(external_max_attempts=2, retry_base_seconds=2)
    env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    request = env.requests.prepare(env.run.id, "user-1", "model", "{}", env.settings)
    settings = Settings(
        _env_file=None,
        data_dir=env.settings.data_dir,
        external_max_attempts=3,
        retry_base_seconds=1,
    )
    assert env.requests.prepare(env.run.id, "user-1", "model", "{}", settings) == request
    env.requests.start(request.id, "user-1")
    waiting = env.requests.fail_attempt(
        request.id,
        "user-1",
        FailureInfo(code="TEMPORARY", category="transient", retryable=True),
        settings,
    )
    assert waiting.retry_at == env.clock.now() + timedelta(seconds=2)
    env.clock.advance(2)
    env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(model_requests=14))
    with pytest.raises(BudgetExceededError):
        env.requests.start(request.id, "user-1")
    assert env.requests.get(request.id, "user-1").attempts == 1


@pytest.mark.parametrize("kind,call_id", [("invalid", None), ("tool", None), ("model", "bad")])
def test_request_kind_validation(harness, kind, call_id):
    env = harness()
    with pytest.raises(ValueError):
        env.requests.prepare(env.run.id, "user-1", kind, "{}", env.settings, call_id=call_id)


def test_request_requires_running_state(harness):
    env = harness()
    with pytest.raises(InvalidRunStateError):
        env.requests.prepare(env.run.id, "user-1", "model", "{}", env.settings)


def test_http_date_generated_by_standard_library():
    clock = Clock()
    assert (
        retry_after_seconds(
            format_datetime(clock.now() + timedelta(seconds=20), usegmt=True), clock.now()
        )
        == 20
    )


def test_reserved_last_decision_honors_long_server_wait(harness, response_factory):
    env = harness(max_model_requests=2)

    def limited(request):
        raise ModelFailure("LIMITED", retryable=True, retry_after="8")

    first = ScriptedModel([limited])
    waiting = drive(env, first)
    assert waiting.status == "waiting_retry" and env.clock.waits == []
    request = env.requests.active_model(env.run.id, "user-1")
    assert request.stop_reason == "decision_reserved"
    assert drive(env, ScriptedModel([])).status == "waiting_retry"
    env.clock.advance(8)
    reopen(env)
    second = ScriptedModel([submit(response_factory)])
    completed = drive(env, second)
    assert completed.status == "completed" and completed.budget_usage.model_requests == 2
    assert second.requests[0] != first.requests[0]
    assert env.requests.get(request.id, "user-1").attempts == 1
    assert notice_count(env, "retry_notice") == 1


def test_deadline_after_short_wait_blocks_reserved_decision(harness):
    env = harness(max_model_requests=2)

    async def sleep(seconds):
        env.clock.advance(600)

    env.executor.sleep = sleep
    model = ScriptedModel([unavailable])
    assert drive(env, model).stop_reason == "budget_deadline"
    assert len(model.requests) == 1


def test_failed_final_decision_stops_without_new_request(harness):
    env = harness(max_model_requests=2)
    model = ScriptedModel([unavailable, unavailable])
    assert drive(env, model).stop_reason == "budget_model_requests"
    assert len(model.requests) == 2


def test_oversized_fatal_tool_feedback_keeps_dependency_classification(harness, response_factory):
    env = harness(max_tool_result_chars=256)

    async def handler(context, parameters):
        raise ToolFailure("FATAL", "依赖服务鉴权失败" * 30, category="authentication")

    env.registry.register(
        ToolDefinition("fatal", "测试服务", ToolParameters, handler, retry_safe=True)
    )
    model = ScriptedModel([response_factory(("fatal", "fatal", {}))])
    assert drive(env, model).stop_reason == "tool_dependency_authentication"
    assert env.calls.get_call(env.run.id, "user-1", "fatal").result.error.code == "RESULT_TOO_LARGE"


def test_expired_run_stops_before_tool_attempt(harness, response_factory):
    env = harness()

    def expired(request):
        env.clock.advance(600)
        return response_factory(("expired", "web_search", {"query": "AI"}))

    failed = drive(env, ScriptedModel([expired]))
    assert failed.stop_reason == "budget_deadline" and failed.budget_usage.tool_calls == 0
    assert env.calls.get_call(env.run.id, "user-1", "expired").started_at is None


def test_result_saved_after_failed_attempt_is_reused_without_reexecuting(
    harness, response_factory, monkeypatch
):
    env = harness()
    original = env.executor.finish

    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted before final feedback")

    monkeypatch.setattr(env.executor, "finish", interrupted)
    # Non-retryable candidate failure is already durable before final pairing.
    with pytest.raises(RuntimeError):
        drive(
            env,
            ScriptedModel([response_factory(("bad", "fetch_url", {"candidate_id": "missing"}))]),
        )
    request = env.requests.for_tool(env.run.id, "user-1", "bad")
    assert request.status == "completed" and request.attempts == 1
    monkeypatch.setattr(env.executor, "finish", original)
    reopen(env)
    assert drive(env, ScriptedModel([submit(response_factory)])).status == "completed"
    assert env.requests.get(request.id, "user-1").attempts == 1
    assert env.runs.get_run(env.run.id, "user-1").budget_usage.fetches == 1


def test_failure_before_deadline_with_retry_beyond_deadline_pairs_and_stops(
    harness, response_factory
):
    env = harness()

    async def handler(context, parameters):
        raise ToolFailure("LIMITED", "临时限流", retryable=True, retry_after="600")

    env.registry.register(
        ToolDefinition("limited", "测试服务", ToolParameters, handler, retry_safe=True)
    )
    model = ScriptedModel([response_factory(("limited", "limited", {}))])
    failed = drive(env, model)
    assert failed.stop_reason == "budget_deadline" and env.clock.waits == []
    assert env.requests.for_tool(env.run.id, "user-1", "limited").attempts == 1
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()


def test_failed_action_streak_counts_varying_error_codes(harness, response_factory):
    env = harness()
    attempts = []

    async def handler(context, parameters):
        attempts.append(True)
        return ToolResult.failure(f"FAIL_{len(attempts)}", "同一动作失败")

    env.registry.register(ToolDefinition("varying", "测试服务", ToolParameters, handler))
    steps = [response_factory((f"a-{n}", "varying", {})) for n in range(4)]
    assert drive(env, ScriptedModel(steps)).stop_reason == "repeated_failed_action"
    assert len(attempts) == 3 and notice_count(env, "repeat_warning") == 1


def test_waiting_request_input_size_change_stops_without_truncation(harness, response_factory):
    env = harness()

    def limited(request):
        raise ModelFailure("LIMITED", retryable=True, retry_after="8")

    drive(env, ScriptedModel([limited]))
    env.clock.advance(8)
    env.settings = Settings(
        _env_file=None, data_dir=env.settings.data_dir, max_model_input_chars=10
    )
    reopen(env)
    model = ScriptedModel([submit(response_factory)])
    assert drive(env, model).stop_reason == "model_input_too_large" and model.requests == []
    assert env.requests.active_model(env.run.id, "user-1") is None
    assert len(env.conversations.messages(env.run.id, "user-1")) == 2


def test_tool_attempt_failure_rolls_back_request_result_state_and_feedback(
    harness, response_factory, monkeypatch
):
    env = harness()
    original = env.runs._append_event

    def broken(connection, run_id, kind, at, data):
        if kind == "tool_feedback_saved":
            raise RuntimeError("feedback interrupted")
        original(connection, run_id, kind, at, data)

    monkeypatch.setattr(env.runs, "_append_event", broken)
    with pytest.raises(RuntimeError):
        drive(env, ScriptedModel([submit(response_factory)]))
    request = env.requests.for_tool(env.run.id, "user-1", "submit")
    assert request.status == "executing"
    assert env.calls.get_call(env.run.id, "user-1", "submit").status == "executing"
    assert env.runs.get_run(env.run.id, "user-1").status == "running"
    assert env.conversations.messages(env.run.id, "user-1")[-1].role == "assistant"


def test_deleting_run_cascades_requests_and_notices(harness, response_factory):
    env = harness(max_model_requests=2)
    drive(env, ScriptedModel([submit(response_factory)]))
    with env.database.transaction() as connection:
        connection.execute("DELETE FROM runs WHERE id = ?", (env.run.id,))
        for table in ("run_messages", "model_responses", "external_requests", "harness_notices"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_phase_one_feedback_retry_and_recovery_trace(harness, response_factory):
    env = harness()

    def limited(request):
        assert observation(request)[1].data["items"] == []
        raise ModelFailure("LIMITED", retryable=True, retry_after="8")

    first = ScriptedModel(
        [
            response_factory(
                ("zero", "web_search", {"query": "zero_results"}), reasoning="保留思考字段"
            ),
            limited,
        ]
    )
    waiting = drive(env, first)
    env.clock.advance(8)
    reopen(env)

    def change_query(request):
        assert observation(request)[1].data["items"] == []
        assert any(m.get("reasoning_content") == "保留思考字段" for m in request.wire_messages())
        return response_factory(
            ("english", "web_search", {"query": "open source AI", "language": "en"})
        )

    def fetch(request):
        assert observation(request)[1].data["items"]
        return response_factory(("bad-fetch", "fetch_url", {"candidate_id": "missing"}))

    def change_source(request):
        assert observation(request)[1].error.code == "CANDIDATE_NOT_FOUND"
        return response_factory(("good-fetch", "fetch_url", {"candidate_id": "fixture-model"}))

    def draft(request):
        assert "8B" in observation(request)[1].data["text"]
        return submit(response_factory, title="simulate_rejection")

    def revise(request):
        assert observation(request)[1].error.code == "BRIEF_REJECTED"
        return submit(response_factory, call_id="revision", title="修订后的中文测试稿")

    second = ScriptedModel([change_query, fetch, change_source, draft, revise])
    completed = drive(env, second)
    assert completed.status == "completed"
    assert completed.budget_usage.model_requests == 7
    assert (
        completed.budget_usage.searches,
        completed.budget_usage.fetches,
        completed.budget_usage.tool_calls,
    ) == (2, 2, 6)
    assert completed.deadline_at == waiting.deadline_at
    assert second.requests[0] == first.requests[-1]
    final_messages = env.conversations.messages(env.run.id, "user-1")
    ModelRequest(messages=final_messages, tools=tuple(env.registry.schemas()))
    assert [m.tool_call_id for m in final_messages if isinstance(m, ToolMessage)] == [
        "zero",
        "english",
        "bad-fetch",
        "good-fetch",
        "submit",
        "revision",
    ]
    assert "fixture-deep-key" not in str(env.runs.list_events(env.run.id, "user-1", limit=500))


def test_fatal_model_failure_and_stop_are_one_transaction(harness, monkeypatch):
    env = harness()
    original = env.runs._transition

    def interrupted(connection, run, change, at):
        if change.status == "failed":
            raise RuntimeError("stop interrupted")
        return original(connection, run, change, at)

    monkeypatch.setattr(env.runs, "_transition", interrupted)

    def fatal(request):
        raise ModelFailure("FATAL", category="authentication")

    with pytest.raises(RuntimeError):
        drive(env, ScriptedModel([fatal]))
    request = env.requests.active_model(env.run.id, "user-1")
    assert request.status == "executing" and request.failure is None
    assert env.runs.get_run(env.run.id, "user-1").status == "running"


def test_truncated_response_remains_stopped_after_reopen(harness, response_factory):
    env = harness()
    run = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    from app.agent.prompts import initial_messages

    env.conversations.initialize(run.id, "user-1", initial_messages(run))
    response = response_factory(
        ("truncated", "web_search", {"query": "AI"}), finish_reason="length"
    )
    env.conversations.save_response(run.id, "user-1", response)
    reopen(env)
    assert drive(env, ScriptedModel([])).stop_reason == "model_incomplete_response"
    assert env.calls.get_call(env.run.id, "user-1", "truncated").started_at is None
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()
