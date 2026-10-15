import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from pydantic import ValidationError

import app.storage.database as database_module
from app.agent.prompts import initial_messages
from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.harness.execution import LeaseLostError, execution_scope
from app.storage.database import Database
from app.storage.leases import LeaseStore
from app.storage.models import BudgetCharge, RunRequest, StateChange
from app.worker.__main__ import run as run_cli
from app.worker.service import Worker
from tests.test_agent_runner import submit
from tests.test_harness import harness as _harness

harness = _harness


def worker(env, model, **kwargs):
    return Worker(AgentRunner(model, env.executor, env.conversations, env.settings), **kwargs)


def claim(env, owner="old"):
    store = LeaseStore(env.runs, env.settings)
    store.touch_worker(owner)
    return store, store.claim_next(owner)


def begin(env):
    current = env.runs.get_run(env.run.id, "user-1")
    if current.status == "queued":
        current = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    env.conversations.initialize(env.run.id, "user-1", initial_messages(current))
    return current


def start_call(env, response):
    begin(env)
    env.conversations.save_response(env.run.id, "user-1", response)
    invocation = response.message.tool_calls[0].invocation()
    definition = env.registry.get(invocation.name)
    request = env.requests.prepare(
        env.run.id,
        "user-1",
        "tool",
        invocation.model_dump_json(),
        env.settings,
        call_id=invocation.tool_call_id,
        retry_safe=definition.retry_safe,
    )
    env.requests.start(request.id, "user-1", tool_dimension=definition.budget_dimension)
    return invocation, request


def test_claim_is_atomic_and_released_by_token_only(harness):
    env = harness()
    store = LeaseStore(env.runs, env.settings)
    for name in ("first", "second"):
        store.touch_worker(name)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(store.claim_next, ("first", "second")))
    assert sum(lease is not None for lease in leases) == 1
    owner = next(lease for lease in leases if lease is not None)
    assert not store.release(owner.model_copy(update={"token": "0" * 32}))
    assert store.release(owner)
    assert store.claim_next("second") is not None


def test_expired_owner_cannot_renew_write_or_publish(harness, tmp_path):
    env = harness(worker_lease_seconds=6, worker_heartbeat_seconds=1)
    begin(env)
    store, old = claim(env)
    env.clock.advance(7)
    store.touch_worker("new")
    current = store.claim_next("new")
    assert current.token != old.token and current.run_id == old.run_id
    with pytest.raises(LeaseLostError):
        store.heartbeat(old)
    before = env.runs.get_run(env.run.id, "user-1")
    with execution_scope(store.execution(old)), pytest.raises(LeaseLostError):
        env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(tool_calls=1))
    from app.storage.snapshots import SnapshotFiles

    target = tmp_path / "snapshot.txt"
    with execution_scope(store.execution(old)), pytest.raises(LeaseLostError):
        SnapshotFiles(env.settings.data_dir, 100).publish(target, b"old")
    assert not target.exists() and env.runs.get_run(env.run.id, "user-1") == before
    assert not store.release(old)
    with execution_scope(store.execution(current)):
        env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(tool_calls=1))


def test_claim_required_for_mutation_but_status_can_be_read(harness):
    env = harness()
    store, lease = claim(env)
    assert env.runs.get_run(env.run.id, "user-1").status == "queued"
    with pytest.raises(LeaseLostError):
        env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    with execution_scope(store.execution(lease)):
        begin(env)
    assert store.release(lease)


def test_claim_cannot_mutate_another_run_or_database(harness, tmp_path):
    env = harness()
    second = env.runs.create_run(
        RunRequest(user_id="user-1", subscription_id="another", subscription=env.run.subscription)
    )
    store, lease = claim(env)
    other = Database(tmp_path / "other.sqlite3")
    other.initialize()
    foreign_id = second.id if lease.run_id != second.id else env.run.id
    with execution_scope(store.execution(lease)):
        with pytest.raises(LeaseLostError):
            env.runs.transition(foreign_id, "user-1", StateChange(status="running"))
        with pytest.raises(LeaseLostError), other.transaction():
            pass


def test_worker_runs_queued_task_and_releases_claim(harness, response_factory):
    env = harness()
    done = asyncio.run(worker(env, ScriptedModel([submit(response_factory)])).run_once())
    assert done.status == "completed" and done.budget_usage.model_requests == 1
    with env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_leases").fetchone()[0] == 0


def test_saved_response_continues_without_new_model_request(harness, response_factory):
    env = harness()
    begin(env)
    env.runs.consume_budget(env.run.id, "user-1", BudgetCharge(model_requests=1))
    env.conversations.save_response(env.run.id, "user-1", submit(response_factory))
    before = env.runs.get_run(env.run.id, "user-1")
    model = ScriptedModel([])
    done = asyncio.run(worker(env, model).run_once())
    assert done.status == "completed" and not model.requests
    assert done.budget_usage.model_requests == 1 and done.budget_usage.tool_calls == 1
    assert done.window_end == before.window_end and done.deadline_at == before.deadline_at


def test_completed_call_feedback_is_paired_without_reexecution(harness, response_factory):
    env = harness()
    begin(env)
    response = submit(response_factory)
    env.conversations.save_response(env.run.id, "user-1", response)
    asyncio.run(
        env.executor.execute(env.run.id, "user-1", response.message.tool_calls[0].invocation())
    )
    before = env.runs.get_run(env.run.id, "user-1").budget_usage
    done = asyncio.run(worker(env, ScriptedModel([])).run_once())
    assert done.status == "completed" and done.budget_usage == before
    ModelRequest(messages=env.conversations.messages(env.run.id, "user-1"), tools=())


@pytest.mark.parametrize("attempts", [1, 3])
def test_unknown_model_response_retries_bounded_and_never_refunds_budget(
    harness, response_factory, attempts
):
    env = harness(external_max_attempts=attempts)
    begin(env)
    request = ModelRequest(
        messages=env.conversations.messages(env.run.id, "user-1"),
        tools=tuple(env.registry.schemas()),
    )
    saved = env.requests.prepare(
        env.run.id, "user-1", "model", request.model_dump_json(), env.settings
    )
    env.requests.start(saved.id, "user-1")
    store, old = claim(env)
    env.clock.advance(31)
    model = ScriptedModel([submit(response_factory)])
    done = asyncio.run(worker(env, model).run_once())
    assert done.budget_usage.model_requests == (2 if attempts > 1 else 1)
    assert done.status == ("completed" if attempts > 1 else "failed")
    if attempts == 1:
        assert done.stop_reason == "model_recovery_exhausted" and not model.requests
    assert not store.release(old)


def test_unknown_network_tool_retries_same_call_without_duplicate_logical_charge(
    harness, response_factory
):
    env = harness()
    inv, saved = start_call(env, response_factory(("search", "web_search", {"query": "AI"})))
    _, old = claim(env)
    env.clock.advance(31)
    before = env.runs.get_run(env.run.id, "user-1")
    done = asyncio.run(worker(env, ScriptedModel([submit(response_factory)])).run_once())
    assert done.status == "completed"
    assert done.budget_usage.searches == before.budget_usage.searches == 1
    assert done.budget_usage.tool_calls == 2
    assert env.requests.get(saved.id, "user-1").attempts == 2
    assert env.calls.get_call(env.run.id, "user-1", inv.tool_call_id).result.ok
    assert old.run_id == done.id


def test_unsafe_unknown_call_returns_explicit_failure(harness, response_factory):
    env = harness()
    invocation, _ = start_call(env, submit(response_factory, "interrupted"))
    done = asyncio.run(worker(env, ScriptedModel([submit(response_factory, "new")])).run_once())
    assert done.status == "completed"
    assert (
        env.calls.get_call(env.run.id, "user-1", invocation.tool_call_id).result.error.code
        == "TOOL_INTERRUPTED_UNSAFE"
    )


def test_expired_deadline_stops_and_pairs_all_calls(harness, response_factory):
    env = harness()
    invocation, _ = start_call(env, response_factory(("search", "web_search", {"query": "AI"})))
    before = env.runs.get_run(env.run.id, "user-1")
    env.clock.advance(601)
    model = ScriptedModel([])
    done = asyncio.run(worker(env, model).run_once())
    assert done.status == "failed" and done.stop_reason == "budget_deadline"
    assert done.budget_usage == before.budget_usage and done.deadline_at == before.deadline_at
    assert (
        env.calls.get_call(done.id, "user-1", invocation.tool_call_id).result.error.code
        == "BUDGET_EXCEEDED"
    )
    ModelRequest(messages=env.conversations.messages(done.id, "user-1"), tools=())


def test_due_retry_yields_worker_to_other_queued_tasks(harness, response_factory):
    async def run():
        env = harness(retry_inline_max_seconds=0)
        invocation, request = start_call(
            env, response_factory(("search", "web_search", {"query": "AI"}))
        )
        from app.harness.failures import FailureInfo

        env.requests.fail_attempt(
            request.id,
            "user-1",
            FailureInfo(code="RATE_LIMIT", retryable=True, retry_after="10"),
            env.settings,
        )
        later = env.runs.create_run(
            RunRequest(user_id="user-1", subscription_id="next", subscription=env.run.subscription)
        )
        w = worker(env, ScriptedModel([submit(response_factory), submit(response_factory)]))
        done = await w.run_once()
        assert done.id == later.id and done.status == "completed"
        assert await w.run_once() is None
        env.clock.advance(10)
        done = await w.run_once()
        assert done.id == env.run.id and done.status == "completed"
        assert env.calls.get_call(env.run.id, "user-1", invocation.tool_call_id).result.ok

    asyncio.run(run())


def test_heartbeat_runs_during_model_wait(harness, response_factory, monkeypatch):
    async def run():
        env = harness(worker_lease_seconds=3, worker_heartbeat_seconds=0.1)
        entered, finish = asyncio.Event(), asyncio.Event()

        class Slow:
            async def complete(self, request):
                entered.set()
                await finish.wait()
                return submit(response_factory)

        w = worker(env, Slow())
        original = w.leases.heartbeat
        pulses = []

        def heartbeat(lease):
            env.clock.advance(0.2)
            original(lease)
            pulses.append(lease.token)

        monkeypatch.setattr(w.leases, "heartbeat", heartbeat)
        task = asyncio.create_task(w.run_once())
        await entered.wait()
        for _ in range(40):
            if len(pulses) >= 2:
                break
            await asyncio.sleep(0.05)
        assert len(pulses) >= 2
        finish.set()
        assert (await task).status == "completed"

    asyncio.run(run())


def test_resumed_old_model_cannot_save_after_takeover(harness, response_factory):
    async def run():
        env = harness(worker_lease_seconds=3, worker_heartbeat_seconds=0.1)
        entered, finish = asyncio.Event(), asyncio.Event()

        class Old:
            async def complete(self, request):
                entered.set()
                await finish.wait()
                return submit(response_factory, "old-submit")

        old = worker(env, Old())
        task = asyncio.create_task(old.run_once())
        await entered.wait()
        env.clock.advance(4)
        new = worker(env, ScriptedModel([submit(response_factory, "new-submit")]))
        assert (await new.run_once()).status == "completed"
        finish.set()
        with pytest.raises(LeaseLostError):
            await task
        responses = env.conversations.responses(env.run.id, "user-1")
        assert len(responses) == 1 and responses[0].message.tool_calls[0].id == "new-submit"

    asyncio.run(run())


def test_worker_serve_stops_cleanly_and_marks_heartbeat(harness, response_factory):
    async def run():
        env = harness(worker_poll_seconds=0.05)
        w = worker(env, ScriptedModel([submit(response_factory)]))
        stop = asyncio.Event()
        task = asyncio.create_task(w.serve(stop))
        for _ in range(50):
            if env.runs.get_run(env.run.id, "user-1").status == "completed":
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task
        with env.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM worker_instances WHERE id=?", (w.worker_id,)
            ).fetchone()
            assert row["stopped_at"] is not None

    asyncio.run(run())


def test_cli_shared_runtime_empty_and_missing_model(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path)
    assert asyncio.run(run_cli(True, config)) is None
    from app.storage.runs import RunStore

    runs = RunStore(Database(config.database_path), config)
    from app.storage.models import SubscriptionSnapshot

    pending = runs.create_run(
        RunRequest(
            user_id="u",
            subscription_id="s",
            subscription=SubscriptionSnapshot(
                nickname="测试", email="test@example.com", topics=("AI",)
            ),
        )
    )
    done = asyncio.run(run_cli(True, config))
    assert done.id == pending.id and done.stop_reason == "model_dependency_configuration"


@pytest.mark.parametrize(
    "values",
    [
        {"worker_lease_seconds": 2},
        {"worker_heartbeat_seconds": 0},
        {"worker_poll_seconds": 0},
        {"worker_lease_seconds": 3, "worker_heartbeat_seconds": 2},
    ],
)
def test_worker_timing_is_validated(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_v11_migration_rollback_preserves_runs(harness, monkeypatch):
    env = harness()
    before = env.runs.get_run(env.run.id, "user-1")
    with env.database.connection() as connection:
        connection.execute("DROP TABLE run_leases")
        connection.execute("DROP TABLE worker_instances")
        connection.execute("PRAGMA user_version=10")
    schema = database_module.SCHEMA_V11
    monkeypatch.setattr(database_module, "SCHEMA_V11", (*schema, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        env.database.initialize()
    assert env.runs.get_run(env.run.id, "user-1") == before
    with env.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    monkeypatch.setattr(database_module, "SCHEMA_V11", schema)
    env.database.initialize()
    env.database.initialize()
    assert env.runs.get_run(env.run.id, "user-1") == before


def test_run_once_cancellation_preserves_uncertain_request_for_takeover(harness, response_factory):
    async def run():
        env = harness()
        entered = asyncio.Event()

        class Waiting:
            async def complete(self, request):
                entered.set()
                await asyncio.Event().wait()

        old = worker(env, Waiting())
        task = asyncio.create_task(old.run_once())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.requests.active_model(env.run.id, "user-1").status == "executing"
        assert (
            await worker(env, ScriptedModel([submit(response_factory)])).run_once()
        ).status == "completed"

    asyncio.run(run())


def test_one_worker_serializes_research_tasks(harness, response_factory):
    async def run():
        env = harness()
        env.runs.create_run(
            RunRequest(
                user_id="user-1", subscription_id="second", subscription=env.run.subscription
            )
        )
        active, peak = 0, 0

        class Model:
            async def complete(self, request):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return submit(response_factory)

        w = worker(env, Model())
        results = await asyncio.gather(w.run_once(), w.run_once())
        assert peak == 1 and all(result.status == "completed" for result in results)

    asyncio.run(run())


def test_heartbeat_loss_cancels_work_and_serve_exits_cleanly(harness, monkeypatch):
    async def run():
        env = harness(worker_lease_seconds=3, worker_heartbeat_seconds=0.1)
        stopped = asyncio.Event()

        class Waiting:
            async def complete(self, request):
                await asyncio.Event().wait()

        w = worker(env, Waiting())

        def lost(lease):
            stopped.set()
            raise LeaseLostError("fixture lost")

        monkeypatch.setattr(w.leases, "heartbeat", lost)
        await w.serve(stopped)
        assert env.requests.active_model(env.run.id, "user-1").status == "executing"
        with env.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM run_leases").fetchone()[0] == 0

    asyncio.run(run())


def test_recovery_keeps_last_review_model_slot(harness):
    env = harness(max_model_requests=2)
    definition = env.registry.get("submit_brief")
    env.registry._definitions["submit_brief"] = replace(definition, reserve_model_requests=1)
    begin(env)
    request = ModelRequest(
        messages=env.conversations.messages(env.run.id, "user-1"),
        tools=tuple(env.registry.schemas()),
    )
    saved = env.requests.prepare(
        env.run.id, "user-1", "model", request.model_dump_json(), env.settings
    )
    env.requests.start(saved.id, "user-1")
    model = ScriptedModel([])
    done = asyncio.run(worker(env, model).run_once())
    assert done.status == "failed" and done.stop_reason == "budget_model_requests"
    assert not model.requests and done.budget_usage.model_requests == 1


def test_old_tool_context_cannot_be_reused_under_new_owner(harness):
    from app.tools.context import bind_context

    env = harness()
    store, old = claim(env)
    with execution_scope(store.execution(old)):
        context = bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1")
    env.clock.advance(31)
    store.touch_worker("new")
    new = store.claim_next("new")
    with execution_scope(store.execution(new)), pytest.raises(LeaseLostError):
        context.resolve_path("scratch/a", write=True)


def test_cli_help_is_available_without_starting_services():
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "app.worker", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0 and "--once" in result.stdout


def test_stop_event_cancels_an_inflight_model_request(harness):
    async def run():
        env = harness()
        stop, entered = asyncio.Event(), asyncio.Event()

        class Waiting:
            async def complete(self, request):
                entered.set()
                await asyncio.Event().wait()

        w = worker(env, Waiting())
        task = asyncio.create_task(w.serve(stop))
        await entered.wait()
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert env.requests.active_model(env.run.id, "user-1").status == "executing"
        with env.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM run_leases").fetchone()[0] == 0

    asyncio.run(run())
