import asyncio
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from app.agent.prompts import initial_messages
from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.harness.execution import execution_scope
from app.storage.conversations import ConversationStore
from app.storage.materials import MaterialStore
from app.tools.context import bind_context
from app.tools.files import register_file_tools
from app.tools.stubs import register_submission_fixture
from app.worker.recovery import Recovery
from app.worker.service import Worker
from tests.brief_support import draft, draft_item, review_world
from tests.research_support import ResearchWorld
from tests.test_agent_runner import submit
from tests.test_briefs import brief_env as _brief_env
from tests.test_briefs import invocation
from tests.test_fetch import Pages
from tests.test_fetch import install as install_fetch
from tests.test_harness import Clock
from tests.test_review import ReviewModel
from tests.test_worker import claim, start_call

brief_env = _brief_env


class Interrupted(BaseException):
    pass


def attach(env, executor=None):
    if executor is not None:
        env.executor, env.registry = executor, executor.registry
    env.conversations = ConversationStore(env.calls)
    env.requests = env.conversations.requests
    env.clock = Clock()
    env.clock.at = env.runs.now()
    env.runs.clock = env.clock.now
    env.executor.sleep = env.clock.sleep
    return env


def make_worker(env, model, *, sources=None, materials=None, docker=None):
    runner = AgentRunner(model, env.executor, env.conversations, env.settings)
    return Worker(
        runner, recovery=Recovery(runner, sources=sources, materials=materials, docker=docker)
    )


async def handler(env, call):
    definition = env.registry.get(call.name)
    return await definition.handler(
        bind_context(
            env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id=call.tool_call_id
        ),
        env.executor._arguments(definition, call),
    )


@pytest.mark.parametrize("damage", [None, "index_missing", "body_missing", "body_changed"])
def test_source_files_recover_registration_without_fetching_again(
    tool_env, response_factory, damage
):
    async def run():
        env = tool_env
        pages = Pages(env)
        installed = await install_fetch(env, pages)
        env = attach(env, installed.executor)
        register_submission_fixture(env.registry)
        call, _ = start_call(
            env, response_factory(("fetch", "fetch_url", {"candidate_id": installed.ids[0]}))
        )
        store, old = claim(env)
        with execution_scope(store.execution(old)):
            first = await handler(env, call)
            context = bind_context(
                env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="fetch"
            )
            job = installed.store.files.load_job(context)
            if damage:
                path = context.working_dir / (
                    job.metadata.index_path if damage == "index_missing" else job.metadata.file_path
                )
                if damage == "body_changed":
                    path.write_bytes(path.read_bytes() + b"changed")
                else:
                    path.unlink()
        before = env.runs.get_run(env.run.id, "user-1")
        env.clock.advance(31)
        done = await make_worker(
            env, ScriptedModel([submit(response_factory)]), sources=installed.store
        ).run_once()
        saved = env.calls.get_call(env.run.id, "user-1", "fetch").result
        assert done.status == "completed" and len(pages.seen) == 1
        assert done.window_end == before.window_end and done.deadline_at == before.deadline_at
        assert done.budget_usage.fetches == before.budget_usage.fetches == 1
        if damage in {"body_missing", "body_changed"}:
            assert saved.error.code == "SOURCE_STATE_PROBLEM"
        else:
            assert saved == first
            metadata, _ = installed.store.get_source(env.run.id, "user-1", first.data["source_id"])
            assert metadata == job.metadata

    asyncio.run(run())


def test_registered_source_corruption_stops_recovery_and_pairs_pending_calls(
    tool_env, response_factory
):
    async def run():
        env = tool_env
        installed = await install_fetch(env, Pages(env))
        env = attach(env, installed.executor)
        first = await env.executor.execute(env.run.id, "user-1", invocation({}, "unused"))
        assert not first.ok
        from tests.test_fetch import fetch_call

        fetched = await env.executor.execute(env.run.id, "user-1", fetch_call(installed.ids[0]))
        materials = MaterialStore(installed.store, env.settings)
        register_file_tools(env.registry, materials, env.settings)
        start_call(
            env, response_factory(("read", "read_file", {"path": fetched.data["file_path"]}))
        )
        path = env.settings.data_dir / "runs" / env.run.id / fetched.data["file_path"]
        path.write_bytes(path.read_bytes() + b"damaged")
        done = await make_worker(env, ScriptedModel([]), sources=installed.store).run_once()
        assert done.status == "failed" and done.stop_reason == "recovery_material_state_problem"
        ModelRequest(messages=env.conversations.messages(env.run.id, "user-1"), tools=())

    asyncio.run(run())


@pytest.mark.parametrize("conflict", [False, True])
def test_scratch_write_replay_reuses_intent_or_reports_conflict(
    material_env, response_factory, conflict
):
    async def run():
        env = attach(material_env)
        register_submission_fixture(env.registry)
        call, _ = start_call(
            env,
            response_factory(
                ("write", "write_file", {"path": "scratch/note.txt", "content": "固定笔记\n"})
            ),
        )
        store, old = claim(env)
        with execution_scope(store.execution(old)):
            first = await handler(env, call)
        path = env.root / "scratch/note.txt"
        if conflict:
            path.write_text("外部修改", encoding="utf-8")
        env.clock.advance(31)
        done = await make_worker(
            env, ScriptedModel([submit(response_factory)]), materials=env.materials
        ).run_once()
        assert done.status == "completed"
        result = env.calls.get_call(env.run.id, "user-1", "write").result
        assert result == first if not conflict else result.error.code == "FILE_WRITE_CONFLICT"
        assert path.read_text(encoding="utf-8") == ("外部修改" if conflict else "固定笔记\n")
        written = [
            e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "material_written"
        ]
        assert len(written) == (0 if conflict else 1)

    asyncio.run(run())


def test_discovery_cache_recovery_does_not_repeat_network_or_candidate_ids(
    tool_env, response_factory
):
    async def run():
        env = tool_env
        async with ResearchWorld(env) as world:
            attach(env, world.executor)
            call, request = start_call(
                env, response_factory(("search", "web_search", {"query": "Atlas"}))
            )
            store, old = claim(env)
            with execution_scope(store.execution(old)):
                first = await handler(env, call)
            env.clock.advance(31)
            done = await make_worker(
                env, ScriptedModel([submit(response_factory)]), sources=world.sources
            ).run_once()
            assert done.status == "completed" and len(world.discoveries) == 1
            assert env.calls.get_call(env.run.id, "user-1", "search").result == first
            assert env.requests.get(request.id, "user-1").attempts == 1
            assert done.budget_usage.searches == 1

    asyncio.run(run())


def test_submission_intent_recovery_saves_same_brief_once(brief_env, response_factory):
    async def run():
        ready = brief_env
        env = attach(ready.env, ready.executor)
        call, _ = start_call(
            env, response_factory(("submit", "submit_brief", {"draft": ready.draft}))
        )
        store, old = claim(env)
        with execution_scope(store.execution(old)):
            first = await handler(env, call)
        assert len(ready.reviews) == 1
        env.clock.advance(31)
        done = await make_worker(
            env, ScriptedModel([]), sources=ready.store.rules.sources
        ).run_once()
        assert done.status == "completed" and len(ready.reviews) == 1
        assert env.calls.get_call(env.run.id, "user-1", "submit").result == first
        with env.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM delivered_history").fetchone()[0] == 1

    asyncio.run(run())


@pytest.mark.parametrize("full_budget", [False, True])
def test_saved_review_recovers_without_new_model_request(
    tool_env, response_factory, monkeypatch, full_budget
):
    async def run():
        env, review = tool_env, ReviewModel()
        async with review_world(env, review) as world:
            value = draft(await draft_item(world))
            attach(env, world.executor)
            call, _ = start_call(
                env, response_factory(("submit", "submit_brief", {"draft": value}))
            )
            original = world.briefs.stage
            monkeypatch.setattr(
                world.briefs, "stage", lambda *args, **kwargs: (_ for _ in ()).throw(Interrupted())
            )
            store, old = claim(env)
            with execution_scope(store.execution(old)):
                with pytest.raises(Interrupted):
                    await handler(env, call)
                if full_budget:
                    from app.storage.models import BudgetCharge

                    env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(model_requests=14))
            monkeypatch.setattr(world.briefs, "stage", original)
            env.clock.advance(31)
            done = await make_worker(
                env, ScriptedModel([]), sources=world.sources, materials=world.materials
            ).run_once()
            assert done.status == "completed" and len(review.requests) == 1
            assert done.budget_usage.model_requests == (15 if full_budget else 1)

    asyncio.run(run())


def test_unknown_review_response_retries_and_counts_both_requests(tool_env, response_factory):
    async def run():
        env, review = tool_env, ReviewModel()
        async with review_world(env, review) as world:
            value = draft(await draft_item(world))
            attach(env, world.executor)
            call, _ = start_call(
                env, response_factory(("submit", "submit_brief", {"draft": value}))
            )
            original = review.complete

            async def crash(request):
                raise Interrupted

            review.complete = crash
            store, old = claim(env)
            with execution_scope(store.execution(old)), pytest.raises(Interrupted):
                await handler(env, call)
            review.complete = original
            env.clock.advance(31)
            done = await make_worker(env, ScriptedModel([]), sources=world.sources).run_once()
            assert done.status == "completed" and done.budget_usage.model_requests == 2
            assert len(review.requests) == 1
            with env.database.connection() as connection:
                assert connection.execute("SELECT COUNT(*) FROM submission_jobs").fetchone()[0] == 1

    asyncio.run(run())


def test_only_owned_pending_temporary_files_are_removed(material_env, response_factory):
    async def run():
        env = attach(material_env)
        register_submission_fixture(env.registry)
        env.seed("scratch/.pending-dead", "partial")
        env.seed("scratch/keep.txt", "keep")
        model = ScriptedModel([submit(response_factory)])
        done = await make_worker(env, model, materials=env.materials).run_once()
        assert done.status == "completed"
        assert not (env.root / "scratch/.pending-dead").exists()
        assert (env.root / "scratch/keep.txt").read_text() == "keep"

    asyncio.run(run())


def test_real_process_exit_after_file_replace_is_recovered(material_env, response_factory):
    env = attach(material_env)
    register_submission_fixture(env.registry)
    script = """
import asyncio, os, sys
from app.agent.protocol import AssistantMessage, FunctionCall, ModelResponse, ModelToolCall
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.storage.database import Database
from app.storage.runs import RunStore
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.candidates import CandidateStore
from app.storage.sources import SourceStore
from app.storage.materials import MaterialStore
from app.tools.files import register_file_tools
from app.tools.registry import ToolRegistry
from app.tools.stubs import register_submission_fixture
from app.tools.executor import ToolExecutor
from app.worker.service import Worker
from app.worker.recovery import Recovery
config=Settings(_env_file=None, data_dir=sys.argv[1], worker_lease_seconds=3,
                worker_heartbeat_seconds=0.1)
db=Database(config.database_path); db.initialize()
runs=RunStore(db,config); calls=CallStore(runs)
sources=SourceStore(CandidateStore(calls),config); materials=MaterialStore(sources,config)
registry=ToolRegistry(); register_file_tools(registry,materials,config)
register_submission_fixture(registry)
response=ModelResponse(message=AssistantMessage(tool_calls=(ModelToolCall(id='crash-write',function=FunctionCall(
name='write_file',arguments='{"path":"scratch/hard-crash.txt","content":"durable"}')),)),finish_reason='tool_calls')
original=materials.write
def crash(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(23)
materials.write=crash
runner=AgentRunner(ScriptedModel([response]),ToolExecutor(registry,calls,config),ConversationStore(calls),config)
asyncio.run(Worker(runner,recovery=Recovery(runner,sources=sources,materials=materials)).run_once())
"""
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP"}
    }
    process = subprocess.run(
        [sys.executable, "-c", script, str(env.settings.data_dir)],
        capture_output=True,
        text=True,
        timeout=30,
        env=child_env,
    )
    assert process.returncode == 23, process.stderr
    assert (env.root / "scratch/hard-crash.txt").read_text() == "durable"
    before = env.runs.get_run(env.run.id, "user-1")
    assert env.calls.get_call(env.run.id, "user-1", "crash-write").status == "executing"
    env.clock.at = env.runs.now() + timedelta(seconds=5)
    # The child advanced the real clock; the resumed clock must follow its last update.
    env.clock.at = max(env.clock.at, before.updated_at + timedelta(seconds=5))
    done = asyncio.run(
        make_worker(
            env,
            ScriptedModel([submit(response_factory)]),
            sources=env.materials.sources,
            materials=env.materials,
        ).run_once()
    )
    assert done.status == "completed" and done.budget_usage.tool_calls == 2
    assert done.budget_usage.model_requests == 2 and done.deadline_at == before.deadline_at
    ModelRequest(messages=env.conversations.messages(env.run.id, "user-1"), tools=())


def test_missing_write_intent_does_not_overwrite_existing_file(material_env, response_factory):
    async def run():
        env = attach(material_env)
        register_submission_fixture(env.registry)
        env.seed("scratch/a", "外部内容")
        start_call(
            env, response_factory(("write", "write_file", {"path": "scratch/a", "content": "替换"}))
        )
        done = await make_worker(
            env, ScriptedModel([submit(response_factory)]), materials=env.materials
        ).run_once()
        assert done.status == "completed"
        assert (
            env.calls.get_call(env.run.id, "user-1", "write").result.error.code
            == "FILE_WRITE_STATE_PROBLEM"
        )
        assert (env.root / "scratch/a").read_text(encoding="utf-8") == "外部内容"

    asyncio.run(run())


def test_corrupt_saved_model_request_stops_without_network(material_env, response_factory):
    async def run():
        env = attach(material_env)
        register_submission_fixture(env.registry)
        env.conversations.initialize(env.run.id, "user-1", initial_messages(env.run))
        request = ModelRequest(
            messages=env.conversations.messages(env.run.id, "user-1"),
            tools=tuple(env.registry.schemas()),
        )
        saved = env.requests.prepare(
            env.run.id, "user-1", "model", request.model_dump_json(), env.settings
        )
        env.requests.start(saved.id, "user-1")
        with env.database.connection() as connection:
            connection.execute("UPDATE external_requests SET payload_json='broken'")
        model = ScriptedModel([])
        done = await make_worker(env, model).run_once()
        assert done.status == "failed" and done.stop_reason == "recovery_material_state_problem"
        assert not model.requests

    asyncio.run(run())
