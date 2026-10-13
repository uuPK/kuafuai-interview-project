import asyncio
import json
import sqlite3

import httpx
import pytest

import app.storage.database as database_module
from app.agent.protocol import ModelRequest, ToolMessage
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.providers.bocha import BochaClient
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.models import RunRequest
from app.storage.runs import RunNotFoundError, RunStore
from app.tools.context import bind_context
from app.tools.discovery import SearchParameters
from app.tools.executor import ToolExecutor
from app.tools.models import ToolInvocation, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition, ToolRegistry
from app.tools.search import SearchTool, register_search
from app.tools.stubs import register_submission_fixture
from tests.test_bocha import item, settings, wire
from tests.test_harness import harness as harness


def invocation(call_id="search", **fields):
    return ToolInvocation(
        tool_call_id=call_id,
        name="web_search",
        arguments=json.dumps({"query": "AI", **fields}, ensure_ascii=False),
    )


def install(env, client, config=None):
    config = config or env.settings
    registry = ToolRegistry()
    store = CandidateStore(env.calls)
    register_search(registry, client, store, config)
    return store, ToolExecutor(registry, env.calls, config)


def test_filtering_candidates_and_replay_after_reopen(tool_env):
    env = tool_env
    excluded = env.run.subscription.model_copy(update={"exclude_keywords": ("SPAM",)})
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET subscription_json = ? WHERE id = ?",
            (excluded.model_dump_json(), env.run.id),
        )
    received = []
    payload = wire(
        item(summary="正文发现摘要", datePublished="2026-10-11T10:00:00+08:00"),
        item(url="https://news.example.com/new#another"),
        item(url="https://example.com.evil.com/new"),
        item(url="https://other.example.com/a", snippet="contains spam"),
        item(url="javascript:bad"),
    )

    def handler(request):
        received.append(request)
        assert json.loads(request.content)["query"] == "AI (site:example.com)"
        return httpx.Response(200, json=payload)

    async def run():
        async with BochaClient(
            settings(env.settings.data_dir), transport=httpx.MockTransport(handler)
        ) as client:
            store, executor = install(env, client)
            call = invocation(domains=["example.com"])
            result = await executor.execute(env.run.id, "user-1", call)
            assert result.ok
            assert result.data["filtered"] == {
                "invalid": 1,
                "domain": 1,
                "excluded": 1,
                "duplicate_url": 1,
            }
            candidate_id = result.data["items"][0]["candidate_id"]
            material = store.get_candidate(env.run.id, "user-1", candidate_id).material
            assert material.date_clues == {"datePublished": "2026-10-11T10:00:00+08:00"}
            assert material.language is None
            database = Database(env.database.path)
            database.initialize()
            new_env = type(env)(
                settings=env.settings, calls=CallStore(RunStore(database, env.settings))
            )
            new_store, new_executor = install(new_env, client)
            assert new_store.get_candidate(env.run.id, "user-1", candidate_id).material == material
            assert await new_executor.execute(env.run.id, "user-1", call) == result
            return result

    result = asyncio.run(run())
    assert len(received) == 1
    run = env.runs.get_run(env.run.id, "user-1")
    assert run.budget_usage.searches == run.budget_usage.tool_calls == 1
    event = [
        event
        for event in env.runs.list_events(run.id, "user-1")
        if event.kind == "search_discovered"
    ][0]
    assert event.data["candidate_ids"] == [result.data["items"][0]["candidate_id"]]
    assert event.data["provider_request_id"] == "trace-1"


@pytest.mark.parametrize(
    "payload,domains,reason",
    [
        (wire(), [], None),
        (wire(item(url="https://other.com/a")), ["example.com"], "domain"),
    ],
)
def test_successful_zero_results_with_filter_explanation(tool_env, payload, domains, reason):
    async def run():
        async with BochaClient(
            settings(tool_env.settings.data_dir),
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)),
        ) as client:
            store, executor = install(tool_env, client)
            result = await executor.execute(tool_env.run.id, "user-1", invocation(domains=domains))
            assert result.ok and result.data["items"] == []
            if reason:
                assert result.data["filtered"][reason] == 1
            assert store.discovery(tool_env.run.id, "user-1", "search").result == result

    asyncio.run(run())


def test_size_limit_keeps_full_material_and_reports_omitted(tool_env):
    payload = wire(
        *(item(url=f"https://example.com/{index}", summary="证据" * 2000) for index in range(5))
    )
    config = settings(tool_env.settings.data_dir, max_tool_result_chars=1700)

    async def run():
        async with BochaClient(
            config, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
        ) as client:
            store, executor = install(tool_env, client, config)
            result = await executor.execute(tool_env.run.id, "user-1", invocation())
            assert result.ok and len(result.model_dump_json()) <= 1700
            assert result.data["omitted_count"] == 5 - len(result.data["items"])
            assert result.data["omitted_count"] > 0
            candidate = result.data["items"][0]
            assert candidate["text_truncated"] and len(candidate["summary"]) == 800
            assert (
                len(
                    store.get_candidate(
                        tool_env.run.id, "user-1", candidate["candidate_id"]
                    ).material.summary
                )
                == 4000
            )

    asyncio.run(run())


@pytest.mark.parametrize("empty", [True, False])
def test_tiny_limit_does_not_report_false_zero_results(tool_env, empty):
    config = settings(tool_env.settings.data_dir, max_tool_result_chars=256)

    async def run():
        async with BochaClient(
            config,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=wire() if empty else wire(item()))
            ),
        ) as client:
            store, executor = install(tool_env, client, config)
            result = await executor.execute(tool_env.run.id, "user-1", invocation())
            assert result.error.code == "RESULT_TOO_LARGE"
            assert store.discovery(tool_env.run.id, "user-1", "search") is None

    asyncio.run(run())


def test_candidate_ownership_and_argument_checks_block_network(tool_env):
    seen = []

    async def run():
        async with BochaClient(
            settings(tool_env.settings.data_dir),
            transport=httpx.MockTransport(
                lambda req: seen.append(req) or httpx.Response(200, json=wire(item()))
            ),
        ) as client:
            store, executor = install(tool_env, client)
            denied = await executor.execute(tool_env.run.id, "other-user", invocation("denied"))
            assert denied.error.code == "PERMISSION_DENIED" and seen == []
            invalid = await executor.execute(
                tool_env.run.id, "user-1", invocation("bad", domains=["https://evil.com"])
            )
            assert invalid.error.code == "INVALID_ARGUMENTS" and seen == []
            result = await executor.execute(tool_env.run.id, "user-1", invocation())
            candidate_id = result.data["items"][0]["candidate_id"]
            with pytest.raises(RunNotFoundError):
                store.get_candidate(tool_env.run.id, "other-user", candidate_id)
            other = tool_env.runs.create_run(
                RunRequest(
                    user_id="user-1",
                    subscription_id="sub-1",
                    subscription=tool_env.run.subscription,
                )
            )
            with pytest.raises(LookupError):
                store.get_candidate(other.id, "user-1", candidate_id)
            context = bind_context(
                tool_env.runs, tool_env.settings.data_dir, tool_env.run.id, "user-1"
            )
            with pytest.raises(PermissionError):
                await SearchTool(client, store, tool_env.settings).execute(
                    context, SearchParameters(query="AI")
                )

    asyncio.run(run())
    assert len(seen) == 1


def test_saved_discovery_survives_interruption_before_tool_completion(tool_env, monkeypatch):
    seen = []

    async def run():
        async with BochaClient(
            settings(tool_env.settings.data_dir),
            transport=httpx.MockTransport(
                lambda req: seen.append(req) or httpx.Response(200, json=wire(item()))
            ),
        ) as client:
            store, executor = install(tool_env, client)

            def interrupted(*args, **kwargs):
                raise KeyboardInterrupt

            monkeypatch.setattr(executor, "finish", interrupted)
            with pytest.raises(KeyboardInterrupt):
                await executor.execute(tool_env.run.id, "user-1", invocation())
            cached = store.discovery(tool_env.run.id, "user-1", "search")
            assert cached is not None
            context = bind_context(
                tool_env.runs,
                tool_env.settings.data_dir,
                tool_env.run.id,
                "user-1",
                tool_call_id="search",
            )
            assert (
                await SearchTool(client, CandidateStore(tool_env.calls), tool_env.settings).execute(
                    context, SearchParameters(query="AI")
                )
                == cached.result
            )
            assert (
                tool_env.calls.get_call(tool_env.run.id, "user-1", "search").status == "executing"
            )

    asyncio.run(run())
    assert len(seen) == 1


def test_store_rejects_unexecuted_or_cross_run_candidates_and_keeps_snapshot(tool_env):
    env = tool_env

    async def run():
        async with BochaClient(
            settings(env.settings.data_dir),
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=wire(item()))),
        ) as client:
            store, executor = install(env, client)
            result = await executor.execute(env.run.id, "user-1", invocation())
            candidate = store.get_candidate(
                env.run.id, "user-1", result.data["items"][0]["candidate_id"]
            )
            common = dict(
                provider="bocha",
                provider_request_id="changed",
                raw={"changed": True},
                candidates=(candidate,),
                result=result,
            )
            frozen = store.discovery(env.run.id, "user-1", "search")
            assert store.save(env.run.id, "user-1", "search", **common) == frozen
            with pytest.raises(RunNotFoundError):
                store.save(env.run.id, "other-user", "search", **common)
            with pytest.raises(ValueError, match="successful"):
                store.save(
                    env.run.id,
                    "user-1",
                    "search",
                    **{**common, "result": ToolResult.failure("BAD", "failed")},
                )
            env.calls.prepare_calls(env.run.id, "user-1", [invocation("pending")])
            with pytest.raises(ValueError, match="executing"):
                store.save(env.run.id, "user-1", "pending", **common)
            from app.storage.models import BudgetCharge

            env.calls.start_call(
                env.run.id, "user-1", "pending", BudgetCharge(tool_calls=1, searches=1)
            )
            with pytest.raises(PermissionError):
                store.save(env.run.id, "user-1", "pending", **common)
            assert store.discovery(env.run.id, "user-1", "pending") is None
            assert store.get_candidate(env.run.id, "user-1", candidate.candidate_id) == candidate

    asyncio.run(run())


def test_model_schema_cannot_accept_server_bound_call_id():
    class Parameters(ToolParameters):
        tool_call_id: str

    async def handler(context, parameters):
        return ToolResult.success()

    with pytest.raises(ValueError, match="server-bound"):
        ToolDefinition("injected", "invalid context override", Parameters, handler)


def test_candidate_insert_and_event_failure_roll_back_snapshot(tool_env, monkeypatch):
    async def run():
        async with BochaClient(
            settings(tool_env.settings.data_dir),
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=wire(item()))),
        ) as client:
            store, executor = install(tool_env, client)
            original = tool_env.runs._append_event

            def broken(connection, run_id, kind, *args):
                if kind == "search_discovered":
                    raise RuntimeError("fixture write interrupted")
                return original(connection, run_id, kind, *args)

            monkeypatch.setattr(tool_env.runs, "_append_event", broken)
            result = await executor.execute(tool_env.run.id, "user-1", invocation())
            assert result.error.code == "INTERNAL_TOOL_ERROR"
            assert store.discovery(tool_env.run.id, "user-1", "search") is None
            with tool_env.database.connection() as connection:
                assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0

    asyncio.run(run())


def test_zero_feedback_changes_language_and_pairs_model_messages(agent_env, response_factory):
    env = agent_env
    seen = []

    def http_handler(request):
        query = json.loads(request.content)["query"]
        seen.append(query)
        return httpx.Response(200, json=wire() if query == "AI" else wire(item()))

    def change_action(request):
        feedback = json.loads(
            next(
                message.content
                for message in reversed(request.messages)
                if isinstance(message, ToolMessage)
            )
        )
        assert feedback["ok"] and feedback["data"]["items"] == []
        return response_factory(
            ("english", "web_search", {"query": "open source AI model", "language": "en"})
        )

    def submit(request):
        feedback = json.loads(request.messages[-1].content)
        assert feedback["data"]["items"][0]["candidate_id"].startswith("cand-")
        return response_factory(("submit", "submit_brief", {"draft": {"title": "搜索协议测试"}}))

    async def run():
        async with BochaClient(
            settings(env.settings.data_dir), transport=httpx.MockTransport(http_handler)
        ) as client:
            _, executor = install(env, client)
            register_submission_fixture(executor.registry)
            model = ScriptedModel(
                [response_factory(("first", "web_search", {"query": "AI"})), change_action, submit]
            )
            result = await AgentRunner(model, executor, env.conversations, env.settings).run(
                env.run.id, "user-1"
            )
            assert result.status == "completed"
            assert result.budget_usage.searches == 2 and result.budget_usage.tool_calls == 3
            ModelRequest(
                messages=env.conversations.messages(env.run.id, "user-1"),
                tools=tuple(executor.registry.schemas()),
            )

    asyncio.run(run())
    assert seen == ["AI", "open source AI model"]


def test_rate_limit_waits_and_resumes_same_search_after_reopen(harness, response_factory):
    env = harness()
    config = settings(env.settings.data_dir)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return (
            httpx.Response(
                429, json={"code": 429, "log_id": "rate-trace"}, headers={"retry-after": "8"}
            )
            if len(seen) == 1
            else httpx.Response(200, json=wire(item()))
        )

    async def run():
        async with BochaClient(config, transport=httpx.MockTransport(handler)) as client:
            _, executor = install(env, client, config)
            register_submission_fixture(executor.registry)
            runner = AgentRunner(
                ScriptedModel([response_factory(("search", "web_search", {"query": "AI"}))]),
                executor,
                env.conversations,
                config,
            )
            waiting = await runner.run(env.run.id, "user-1")
            assert waiting.status == "waiting_retry"
            request = executor.requests.for_tool(env.run.id, "user-1", "search")
            assert (
                request.attempts == 1
                and request.failure.diagnostics.provider_request_id == "rate-trace"
            )
            env.clock.advance(8)
            database = Database(env.database.path)
            database.initialize()
            env.runs = RunStore(database, config, clock=env.clock.now)
            env.calls = CallStore(env.runs)
            env.conversations = ConversationStore(env.calls)
            _, executor = install(env, client, config)
            register_submission_fixture(executor.registry)
            model = ScriptedModel(
                [response_factory(("submit", "submit_brief", {"draft": {"title": "重试完成"}}))]
            )
            completed = await AgentRunner(model, executor, env.conversations, config).run(
                env.run.id, "user-1"
            )
            assert completed.status == "completed" and completed.budget_usage.searches == 1
            assert (
                completed.deadline_at == waiting.deadline_at
                and completed.window_end == waiting.window_end
            )
            assert executor.requests.for_tool(env.run.id, "user-1", "search").attempts == 2

    asyncio.run(run())
    assert len(seen) == 2 and seen[0] == seen[1]


@pytest.mark.parametrize("status", [401, 403])
def test_fatal_provider_failure_is_persisted_without_retry(tool_env, status):
    seen = []

    async def run():
        async with BochaClient(
            settings(tool_env.settings.data_dir),
            transport=httpx.MockTransport(
                lambda req: (
                    seen.append(req)
                    or httpx.Response(status, json={"code": status, "msg": "fixture-bocha-key"})
                )
            ),
        ) as client:
            _, executor = install(tool_env, client)
            result = await executor.execute(tool_env.run.id, "user-1", invocation())
            assert not result.ok
            request = executor.requests.for_tool(tool_env.run.id, "user-1", "search")
            assert request.status == "completed" and request.attempts == 1
            assert "fixture-bocha-key" not in request.failure.model_dump_json()

    asyncio.run(run())
    assert len(seen) == 1


def test_offline_startup_exposes_configuration_feedback(tool_env):
    store, executor = install(tool_env, None)
    result = asyncio.run(executor.execute(tool_env.run.id, "user-1", invocation()))
    assert result.error.code == "BOCHA_CONFIGURATION"
    assert store.discovery(tool_env.run.id, "user-1", "search") is None


def test_v5_migration_preserves_v4_runs_and_rolls_back_failure(tool_env, monkeypatch, tmp_path):
    old = Database(tmp_path / "v4.sqlite3")
    statements = database_module.SCHEMA_V5
    monkeypatch.setattr(database_module, "SCHEMA_V6", ())
    monkeypatch.setattr(database_module, "SCHEMA_V7", ())
    monkeypatch.setattr(database_module, "SCHEMA_V8", ())
    monkeypatch.setattr(database_module, "SCHEMA_V9", ())
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 4)
    monkeypatch.setattr(database_module, "SCHEMA_V5", ())
    old.initialize()
    runs = RunStore(old, tool_env.settings)
    run = runs.create_run(
        RunRequest(
            user_id="user-1", subscription_id="sub-1", subscription=tool_env.run.subscription
        )
    )
    calls = CallStore(runs)
    calls.prepare_calls(run.id, "user-1", [invocation()])
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 5)
    monkeypatch.setattr(database_module, "SCHEMA_V5", (*statements, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        old.initialize()
    with old.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='candidates'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V5", statements)
    old.initialize()
    old.initialize()
    assert runs.get_run(run.id, "user-1") == run
    assert calls.get_call(run.id, "user-1", "search").status == "pending"
    with old.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
