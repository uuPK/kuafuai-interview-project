import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.storage.database as database_module
from app.agent.research import HistoryEvent, HistoryReference, ResearchProgress, selection_notes
from app.storage.calls import CallStore
from app.storage.database import Database
from app.storage.models import RunRequest, StateChange
from app.storage.research import ResearchStore
from app.storage.runs import RunNotFoundError, RunStore
from app.tools.context import bind_context
from app.tools.executor import ToolExecutor
from app.tools.files import WriteParameters
from app.tools.models import ToolFailure, ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.research import HistoryParameters, ResearchTools, register_research
from tests.test_fetch import Pages, fetch_call
from tests.test_fetch import install as install_fetch


def invocation(name="update_progress", call_id="progress", **args):
    return ToolInvocation(
        tool_call_id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False)
    )


def setup(env, config=None):
    registry = ToolRegistry()
    store = ResearchStore(env.calls, config or env.settings)
    register_research(registry, store, config or env.settings)
    return SimpleNamespace(
        store=store,
        registry=registry,
        executor=ToolExecutor(registry, env.calls, config or env.settings),
    )


async def seed_history(
    env,
    *,
    age=1,
    user="user-1",
    facts=("模型v1已开放下载",),
    event_id="model",
    topics=("开源模型",),
    version="v1",
    brief_id=None,
):
    at = env.run.window_end - timedelta(days=age)
    runs = RunStore(env.database, env.settings, clock=lambda: at)
    run = runs.create_run(
        RunRequest(user_id=user, subscription_id="past", subscription=env.run.subscription), now=at
    )
    run = runs.transition(run.id, user, StateChange(status="running"))
    # Seed real owned source files through production fetch/registration using HTTP fixtures.
    old = SimpleNamespace(
        settings=env.settings, database=env.database, runs=runs, run=run, calls=CallStore(runs)
    )
    if user != "user-1":
        # The existing fetch fixture uses user-1; seed then transfer the entire historical run.
        with env.database.connection() as connection:
            connection.execute("UPDATE runs SET user_id='user-1' WHERE id=?", (run.id,))
    fetched = await install_fetch(old, Pages(old))
    result = await fetched.executor.execute(run.id, "user-1", fetch_call(fetched.ids[0]))
    meta, _ = fetched.store.get_source(run.id, "user-1", result.data["source_id"])
    if user != "user-1":
        with env.database.connection() as connection:
            connection.execute("UPDATE runs SET user_id=? WHERE id=?", (user, run.id))
    runs.transition(run.id, user, StateChange(status="completed"))
    event = HistoryEvent(
        event_id=event_id,
        title="模型发布",
        topics=topics,
        entities=("Example AI",),
        product_version=version,
        key_facts=facts,
        citations=(
            HistoryReference(
                source_id=meta.source_id,
                start_offset=0,
                end_offset=len(meta.title),
                quote=meta.title,
            ),
        ),
    )
    store = ResearchStore(old.calls, env.settings)
    with env.database.transaction() as connection:
        store.record_delivery(connection, run.id, user, brief_id or "brief-" + run.id, (event,))
    return SimpleNamespace(
        run=run, event=event, store=store, meta=meta, brief_id=brief_id or "brief-" + run.id
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"days": 0},
        {"days": 31},
        {"days": True},
        {"limit": 0},
        {"limit": 21},
        {"limit": "2"},
        {"user_id": "other"},
    ],
)
def test_history_arguments_are_strict(fields):
    with pytest.raises(ValidationError):
        HistoryParameters.model_validate(fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"collected": [" "]},
        {"missing": ["AI", "ai"]},
        {"issues": ["a\nb"]},
        {"collected": ["AI"], "missing": ["ai"]},
        {"issues": ["a" * 301]},
        {"collected": ["a"] * 21},
        {"issues": [str(i) for i in range(11)]},
        {"issues": "a"},
        {"run_id": "other"},
    ],
)
def test_progress_arguments_are_bounded_unique_and_disjoint(fields):
    value = {"collected": [], "missing": [], "issues": [], **fields}
    with pytest.raises(ValidationError):
        ResearchProgress.model_validate_json(json.dumps(value))


def test_progress_atomic_replacement_permissions_and_reopen(tool_env):
    async def run():
        env = tool_env
        prepared = setup(env)
        assert prepared.store.progress(env.run.id, "user-1").missing == ()
        first = invocation(collected=[], missing=["开源模型"], issues=["缺少正文 fixture-deep-key"])
        result = await prepared.executor.execute(env.run.id, "user-1", first)
        assert result.ok and "fixture-deep-key" not in result.model_dump_json()
        assert prepared.store.progress(env.run.id, "user-1").missing == ("开源模型",)
        assert await prepared.executor.execute(env.run.id, "user-1", first) == result
        second = await prepared.executor.execute(
            env.run.id,
            "user-1",
            invocation(call_id="done", collected=["开源模型"], missing=[], issues=[]),
        )
        assert second.ok and not prepared.store.progress(env.run.id, "user-1").issues
        assert env.runs.get_run(env.run.id, "user-1").status == "running"
        denied = await prepared.executor.execute(
            env.run.id, "other", invocation(call_id="denied", collected=[], missing=[], issues=[])
        )
        assert denied.error.code == "PERMISSION_DENIED"
        with pytest.raises(RunNotFoundError):
            prepared.store.progress(env.run.id, "other")
        unknown = await prepared.executor.execute(
            env.run.id,
            "user-1",
            invocation(call_id="unknown", collected=[], missing=["AI芯片"], issues=[]),
        )
        assert unknown.error.code == "PROGRESS_TOPIC_UNKNOWN"
        database = Database(env.database.path)
        database.initialize()
        persisted = ResearchStore(CallStore(RunStore(database, env.settings)), env.settings)
        assert persisted.progress(env.run.id, "user-1").collected == ("开源模型",)
        events = [
            e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "progress_updated"
        ]
        assert len(events) == 2 and events[0].data["basis"] == "model_reported"
        assert env.runs.get_run(env.run.id, "user-1").budget_usage.tool_calls == 3

    asyncio.run(run())


def test_progress_feedback_failure_rolls_back_and_can_finish_same_call(tool_env):
    async def run():
        env = tool_env
        prepared = setup(env)
        call = invocation(collected=[], missing=["开源模型"], issues=["待补查"])

        def fail(*args):
            raise RuntimeError("feedback interrupted")

        with pytest.raises(RuntimeError):
            await prepared.executor.execute(env.run.id, "user-1", call, on_finished=fail)
        assert prepared.store.progress(env.run.id, "user-1").missing == ()
        assert env.calls.get_call(env.run.id, "user-1", "progress").result is None
        assert not [
            e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "progress_updated"
        ]
        result = await prepared.registry.get("update_progress").handler(
            bind_context(
                env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="progress"
            ),
            ResearchProgress(collected=(), missing=("开源模型",), issues=("待补查",)),
        )
        prepared.executor.finish(env.run.id, "user-1", call, result)
        assert prepared.store.progress(env.run.id, "user-1").missing == ("开源模型",)
        assert env.runs.get_run(env.run.id, "user-1").budget_usage.tool_calls == 1

    asyncio.run(run())


def test_progress_small_result_and_unbound_call_do_not_mutate(tool_env):
    async def run():
        env = tool_env
        prepared = setup(env, env.settings.model_copy(update={"max_tool_result_chars": 256}))
        result = await prepared.executor.execute(
            env.run.id,
            "user-1",
            invocation(collected=[], missing=["开源模型"], issues=["问题" * 100]),
        )
        assert result.error.code == "RESULT_TOO_LARGE"
        assert not prepared.store.progress(env.run.id, "user-1").issues
        with pytest.raises(PermissionError):
            await ResearchTools(prepared.store, env.settings).update_progress(
                bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1"),
                ResearchProgress(collected=(), missing=(), issues=()),
            )

    asyncio.run(run())


def test_history_user_time_limit_and_immutable_delivery(tool_env):
    async def run():
        env = tool_env
        latest = await seed_history(env, age=1)
        await seed_history(env, age=2, version="v0")
        await seed_history(env, age=8)
        await seed_history(env, age=1, user="other-user", facts=("其他用户私有事实",))
        prepared = setup(env)
        result = await prepared.executor.execute(
            env.run.id, "user-1", invocation("query_history", "history", days=7, limit=1)
        )
        assert result.ok and result.data["omitted_count"] == 1
        item = result.data["items"][0]
        assert item["product_version"] == "v1" and item["citations"][0]["url"] == latest.meta.url
        assert item["citations"][0]["content_hash"] == latest.meta.content_hash
        assert "其他用户" not in result.model_dump_json()
        assert (
            await prepared.executor.execute(
                env.run.id, "user-1", invocation("query_history", "history", days=7, limit=1)
            )
            == result
        )
        with env.database.transaction() as connection:
            latest.store.record_delivery(
                connection, latest.run.id, "user-1", latest.brief_id, (latest.event,)
            )
            with pytest.raises(ValueError, match="immutable"):
                latest.store.record_delivery(
                    connection,
                    latest.run.id,
                    "user-1",
                    latest.brief_id,
                    (latest.event.model_copy(update={"key_facts": ("改变旧事实",)}),),
                )
        empty = await prepared.executor.execute(
            env.run.id, "user-1", invocation("query_history", "empty", days=1, limit=20)
        )
        assert len(empty.data["items"]) == 1  # inclusive lower bound
        with pytest.raises(RunNotFoundError):
            prepared.store.history(
                replace(
                    bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1"),
                    user_id="other",
                ),
                7,
                10,
            )

    asyncio.run(run())


def test_history_size_limit_and_corrupt_records_are_not_empty(tool_env):
    async def run():
        env = tool_env
        history = await seed_history(env, facts=("事实" * 400,))
        await seed_history(env, age=2)
        prepared = setup(env, env.settings.model_copy(update={"max_tool_result_chars": 1750}))
        result = await prepared.executor.execute(
            env.run.id, "user-1", invocation("query_history", "size")
        )
        assert (
            result.ok
            and len(result.model_dump_json()) <= 1750
            and result.data["omitted_count"] == 1
        )
        small = setup(env, env.settings.model_copy(update={"max_tool_result_chars": 256}))
        result = await small.executor.execute(
            env.run.id, "user-1", invocation("query_history", "small")
        )
        assert result.error.code == "RESULT_TOO_LARGE"
        with env.database.connection() as connection:
            connection.execute(
                "UPDATE delivered_history SET event_json='{}' WHERE run_id=?", (history.run.id,)
            )
        failed = await prepared.executor.execute(
            env.run.id, "user-1", invocation("query_history", "bad")
        )
        assert failed.error.code == "HISTORY_STATE_PROBLEM"

    asyncio.run(run())


def test_history_publisher_requires_completed_run_owned_sources_and_rollback(tool_env):
    async def run():
        env = tool_env
        history = await seed_history(env)
        prepared = setup(env)
        with env.database.transaction() as connection:
            with pytest.raises(PermissionError, match="accepted"):
                prepared.store.record_delivery(
                    connection, env.run.id, "user-1", "bad", (history.event,)
                )
        env.runs.transition(env.run.id, "user-1", StateChange(status="completed"))
        with pytest.raises(PermissionError), env.database.transaction() as connection:
            prepared.store.record_delivery(
                connection, env.run.id, "user-1", "foreign", (history.event,)
            )
        with env.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM delivered_history WHERE run_id=?", (env.run.id,)
                ).fetchone()[0]
                == 0
            )
        with env.database.transaction() as connection:
            with pytest.raises(ValueError, match="identity"):
                prepared.store.record_delivery(
                    connection, env.run.id, "user-1", "", (history.event,)
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "{}",
        '{"items":["a"]}',
        '{"items":[{"title":"AI","topics":[],"selection_reason":"why"}]}',
        json.dumps({"items": [{}] * 6}),
    ],
)
def test_unstructured_notes_do_not_create_selection_claims(content):
    assert selection_notes(content) == []


def test_draft_selection_reason_is_saved_atomically_and_not_delivery(material_env):
    async def run():
        env = material_env
        draft = {
            "items": [
                {
                    "title": "模型发布",
                    "topics": ["开源模型"],
                    "selection_reason": "Apache许可证便于部署 fixture-deep-key",
                    "citations": [],
                    "user_id": "fake",
                }
            ],
            "uncovered_topics": [],
        }
        written = await env.invoke(
            "write_file", path="scratch/brief.json", content=json.dumps(draft)
        )
        assert written.ok
        events = [
            e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "research_selections"
        ]
        assert len(events) == 1 and events[0].data["items"][0]["selection_reason"].endswith(
            "[REDACTED]"
        )
        assert "user_id" not in events[0].data["items"][0]
        assert env.runs.get_run(env.run.id, "user-1").status == "running"
        assert (
            await env.invoke(
                "write_file",
                call_id="material-1",
                path="scratch/brief.json",
                content=json.dumps(draft),
            )
            == written
        )
        assert (
            len(
                [
                    e
                    for e in env.runs.list_events(env.run.id, "user-1")
                    if e.kind == "research_selections"
                ]
            )
            == 1
        )

    asyncio.run(run())


def test_v8_migration_preserves_materials_and_rolls_back(tool_env, monkeypatch):
    env = tool_env
    old = Database(env.settings.data_dir / "v7.sqlite3")
    original = database_module.SCHEMA_V8
    worker_schema = database_module.SCHEMA_V11
    delivery_schema = database_module.SCHEMA_V12
    auth_schema = database_module.SCHEMA_V13
    product_schema = database_module.SCHEMA_V14
    schedule_schema = database_module.SCHEMA_V15
    review_schema = database_module.SCHEMA_V10
    submission_schema = database_module.SCHEMA_V9
    monkeypatch.setattr(database_module, "SCHEMA_V8", ())
    monkeypatch.setattr(database_module, "SCHEMA_V11", ())
    monkeypatch.setattr(database_module, "SCHEMA_V12", ())
    monkeypatch.setattr(database_module, "SCHEMA_V13", ())
    monkeypatch.setattr(database_module, "SCHEMA_V14", ())
    monkeypatch.setattr(database_module, "SCHEMA_V15", ())
    monkeypatch.setattr(database_module, "SCHEMA_V10", ())
    monkeypatch.setattr(database_module, "SCHEMA_V9", ())
    old.initialize()
    with old.connection() as connection:
        connection.execute("PRAGMA user_version=7")
    runs = RunStore(old, env.settings)
    previous = runs.create_run(
        RunRequest(user_id="user-1", subscription_id="old", subscription=env.run.subscription)
    )
    monkeypatch.setattr(database_module, "SCHEMA_V8", (*original, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        old.initialize()
    with old.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='run_progress'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V8", original)
    monkeypatch.setattr(database_module, "SCHEMA_V9", submission_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V10", review_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V11", worker_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V12", delivery_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V13", auth_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V14", product_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V15", schedule_schema)
    old.initialize()
    old.initialize()
    assert runs.get_run(previous.id, "user-1") == previous
    with old.connection() as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == database_module.SCHEMA_VERSION
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "fields",
    [
        {"start_offset": -1},
        {"end_offset": 0},
        {"end_offset": 3},
        {"quote": ""},
        {"quote": "x" * 2001, "end_offset": 2001},
        {"start_offset": True},
    ],
)
def test_history_reference_is_a_bounded_character_range(fields):
    with pytest.raises(ValidationError):
        HistoryReference.model_validate(
            {"source_id": "src", "start_offset": 0, "end_offset": 2, "quote": "中文", **fields}
        )


def test_corrupted_progress_is_a_state_problem_and_casefold_topic_is_canonical(tool_env):
    async def run():
        env = tool_env
        env.run = env.runs.create_run(
            RunRequest(
                user_id="user-1",
                subscription_id="case",
                subscription=env.run.subscription.model_copy(update={"topics": ("AI Chips",)}),
            )
        )
        env.run = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
        prepared = setup(env)
        result = await prepared.executor.execute(
            env.run.id, "user-1", invocation(collected=["ai chips"], missing=[], issues=[])
        )
        assert result.data["progress"]["collected"] == ["AI Chips"]
        with env.database.connection() as connection:
            connection.execute(
                "UPDATE run_progress SET progress_json='bad' WHERE run_id=?", (env.run.id,)
            )
        with pytest.raises(ToolFailure, match="PROGRESS_STATE_PROBLEM"):
            prepared.store.progress(env.run.id, "user-1")

    asyncio.run(run())


@pytest.mark.parametrize("corruption", ["missing_source", "identity", "range", "source_extent"])
def test_history_bad_source_relationship_is_not_silent_empty(tool_env, corruption):
    async def run():
        env = tool_env
        history = await seed_history(env)
        with env.database.connection() as connection:
            if corruption in {"missing_source", "range", "source_extent"}:
                event = history.event.model_dump(mode="json")
                if corruption == "missing_source":
                    event["citations"][0]["source_id"] = "missing-source"
                elif corruption == "range":
                    event["citations"][0]["end_offset"] = 999999
                else:
                    citation = event["citations"][0]
                    citation["start_offset"] = history.meta.char_count
                    citation["end_offset"] = history.meta.char_count + len(citation["quote"])
                connection.execute(
                    "UPDATE delivered_history SET event_json=? WHERE run_id=?",
                    (json.dumps(event), history.run.id),
                )
            else:
                row = connection.execute(
                    "SELECT metadata_json FROM sources WHERE id=?", (history.meta.source_id,)
                ).fetchone()
                metadata = json.loads(row[0])
                metadata["run_id"] = "other-run"
                connection.execute(
                    "UPDATE sources SET metadata_json=? WHERE id=?",
                    (json.dumps(metadata), history.meta.source_id),
                )
        result = await setup(env).executor.execute(
            env.run.id, "user-1", invocation("query_history", "bad")
        )
        assert result.error.code == "HISTORY_STATE_PROBLEM"

    asyncio.run(run())


def test_history_publisher_requires_an_active_transaction(tool_env):
    async def run():
        history = await seed_history(tool_env)
        with tool_env.database.connection() as connection:
            with pytest.raises(ValueError, match="active publisher transaction"):
                history.store.record_delivery(
                    connection, history.run.id, "user-1", history.brief_id, (history.event,)
                )

    asyncio.run(run())


def test_history_validates_all_sources_before_writing_any_event(tool_env):
    async def run():
        env = tool_env
        history = await seed_history(env)
        ref = history.event.citations[0].model_copy(update={"source_id": "missing-source"})
        invalid = history.event.model_copy(update={"citations": (ref,)})
        with env.database.transaction() as connection:
            connection.execute("DELETE FROM delivered_history WHERE run_id=?", (history.run.id,))
            with pytest.raises(PermissionError, match="belong"):
                history.store.record_delivery(
                    connection, history.run.id, "user-1", history.brief_id, (history.event, invalid)
                )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM delivered_history WHERE run_id=?", (history.run.id,)
                ).fetchone()[0]
                == 0
            )
            too_long = ref.model_copy(
                update={
                    "source_id": history.meta.source_id,
                    "start_offset": history.meta.char_count,
                    "end_offset": history.meta.char_count + len(ref.quote),
                }
            )
            with pytest.raises(ValueError, match="exceeds"):
                history.store.record_delivery(
                    connection,
                    history.run.id,
                    "user-1",
                    history.brief_id,
                    (history.event.model_copy(update={"citations": (too_long,)}),),
                )
        with env.database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM delivered_history WHERE run_id=?", (history.run.id,)
                ).fetchone()[0]
                == 0
            )

    asyncio.run(run())


def test_selection_trace_rolls_back_with_feedback_and_recovers_once(material_env):
    async def run():
        env = material_env
        content = json.dumps(
            {
                "items": [
                    {
                        "title": "发布",
                        "topics": ["开源模型"],
                        "selection_reason": "已发布且许可证便于部署",
                    }
                ]
            },
            ensure_ascii=False,
        )
        call = invocation("write_file", "draft", path="scratch/brief.json", content=content)

        def fail(*args):
            raise RuntimeError("feedback failed")

        with pytest.raises(RuntimeError):
            await env.executor.execute(env.run.id, "user-1", call, on_finished=fail)
        assert not [
            e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "research_selections"
        ]
        assert (env.root / "scratch/brief.json").is_file()
        result = await env.registry.get("write_file").handler(
            bind_context(
                env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="draft"
            ),
            WriteParameters(path="scratch/brief.json", content=content),
        )
        env.executor.finish(env.run.id, "user-1", call, result)
        assert await env.executor.execute(env.run.id, "user-1", call) == result
        assert (
            len(
                [
                    e
                    for e in env.runs.list_events(env.run.id, "user-1")
                    if e.kind == "research_selections"
                ]
            )
            == 1
        )

    asyncio.run(run())


def test_history_future_time_cross_user_and_out_of_range_delivery_rejected(tool_env):
    async def run():
        env = tool_env
        history = await seed_history(env)
        with env.database.transaction() as connection:
            with pytest.raises(RunNotFoundError):
                history.store.record_delivery(
                    connection, history.run.id, "other", "x", (history.event,)
                )
            # Same source, new invalid range; no delivery row is written on failure.
            citation = history.event.citations[0]
            long_ref = citation.model_copy(
                update={"end_offset": citation.end_offset + 1, "quote": citation.quote + "x"}
            )
            bad = history.event.model_copy(update={"citations": (long_ref,)})
            # Existing history is immutable before a changed publisher request is considered.
            with pytest.raises(ValueError, match="immutable"):
                history.store.record_delivery(
                    connection, history.run.id, "user-1", history.brief_id, (bad,)
                )
        # Delivery written after the current run's window is excluded even though the run is older.
        with env.database.connection() as connection:
            connection.execute(
                "UPDATE delivered_history SET delivered_at=? WHERE run_id=?",
                ((env.run.window_end + timedelta(hours=1)).isoformat(), history.run.id),
            )
        result = await setup(env).executor.execute(
            env.run.id, "user-1", invocation("query_history", "future")
        )
        assert result.ok and not result.data["items"]

    asyncio.run(run())
