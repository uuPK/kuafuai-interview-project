import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.storage.database as database_module
from app.storage.calls import CallConflictError, CallInProgressError, CallStore
from app.storage.database import Database
from app.storage.models import BudgetCharge
from app.storage.runs import RunNotFoundError
from app.storage.schema import SCHEMA_V1
from app.tools.models import ToolInvocation, ToolResult


def invocation(call_id="call-1", **overrides):
    return ToolInvocation(
        **{
            "tool_call_id": call_id,
            "name": "web_search",
            "arguments": '{"query":"AI"}',
            **overrides,
        }
    )


def test_pending_executing_and_completed_are_durable(tool_env) -> None:
    calls, run = tool_env.calls, tool_env.run
    pending = calls.prepare_calls(run.id, run.user_id, [invocation()])[0]
    assert pending.status == "pending"
    assert pending.result is pending.started_at is pending.finished_at is None
    executing = calls.start_call(run.id, run.user_id, "call-1", BudgetCharge(tool_calls=1))
    assert executing.status == "executing"
    assert executing.started_at is not None
    result = ToolResult.success({"items": []})
    completed = calls.finish_call(run.id, run.user_id, "call-1", result)
    assert completed.status == "completed"
    assert completed.result == result
    assert completed.finished_at >= executing.started_at
    database = Database(tool_env.database.path)
    database.initialize()
    reopened = CallStore(tool_env.runs.__class__(database, tool_env.settings))
    assert reopened.get_call(run.id, run.user_id, "call-1") == completed
    assert [event.kind for event in tool_env.runs.list_events(run.id, run.user_id)][-4:] == [
        "tool_pending",
        "budget_consumed",
        "tool_executing",
        "tool_completed",
    ]


def test_preparing_same_call_is_idempotent_but_new_id_is_a_new_call(tool_env) -> None:
    run, calls = tool_env.run, tool_env.calls
    original = calls.prepare_calls(run.id, run.user_id, [invocation()])
    assert calls.prepare_calls(run.id, run.user_id, [invocation()]) == original
    second = calls.prepare_calls(run.id, run.user_id, [invocation("call-2")])[0]
    assert second.tool_call_id == "call-2"
    assert len(tool_env.runs.list_events(run.id, run.user_id)) == 4


def test_batch_conflict_rolls_back_all_new_calls(tool_env) -> None:
    run, calls = tool_env.run, tool_env.calls
    calls.prepare_calls(run.id, run.user_id, [invocation()])
    with pytest.raises(CallConflictError):
        calls.prepare_calls(
            run.id,
            run.user_id,
            [invocation("new-call"), invocation(arguments='{"query":"changed"}')],
        )
    with pytest.raises(LookupError):
        calls.get_call(run.id, run.user_id, "new-call")
    with pytest.raises(CallConflictError):
        calls.prepare_calls(run.id, run.user_id, [invocation(), invocation()])


def test_finishing_cannot_replace_saved_results_or_fabricate_success(tool_env) -> None:
    run, calls = tool_env.run, tool_env.calls
    calls.prepare_calls(run.id, run.user_id, [invocation()])
    with pytest.raises(CallConflictError):
        calls.finish_call(run.id, run.user_id, "call-1", ToolResult.success())
    failed = ToolResult.failure("BUDGET_EXCEEDED", "预算耗尽。")
    complete = calls.finish_call(run.id, run.user_id, "call-1", failed)
    assert calls.finish_call(run.id, run.user_id, "call-1", failed) == complete
    with pytest.raises(CallConflictError):
        calls.finish_call(run.id, run.user_id, "call-1", ToolResult.success())


@pytest.mark.parametrize("operation", ["prepare", "get", "start", "finish"])
def test_call_storage_checks_user_ownership(tool_env, operation) -> None:
    run, calls = tool_env.run, tool_env.calls
    calls.prepare_calls(run.id, run.user_id, [invocation()])
    with pytest.raises(RunNotFoundError):
        if operation == "prepare":
            calls.prepare_calls(run.id, "other", [invocation()])
        elif operation == "get":
            calls.get_call(run.id, "other", "call-1")
        elif operation == "start":
            calls.start_call(run.id, "other", "call-1", BudgetCharge(tool_calls=1))
        else:
            calls.finish_call(run.id, "other", "call-1", ToolResult.success())


def test_only_one_executor_can_start_a_call(tool_env) -> None:
    run, calls = tool_env.run, tool_env.calls
    calls.prepare_calls(run.id, run.user_id, [invocation()])

    def start(_):
        try:
            calls.start_call(run.id, run.user_id, "call-1", BudgetCharge(tool_calls=1))
            return "started"
        except CallInProgressError:
            return "in_progress"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start, range(2))) == ["in_progress", "started"]
    assert tool_env.runs.get_run(run.id, run.user_id).budget_usage.tool_calls == 1


def test_call_start_and_budget_are_rolled_back_together(tool_env, monkeypatch) -> None:
    run, calls = tool_env.run, tool_env.calls
    calls.prepare_calls(run.id, run.user_id, [invocation()])
    append = tool_env.runs._append_event

    def fail(connection, run_id, kind, at, data):
        if kind == "tool_executing":
            raise RuntimeError("simulated persistence failure")
        append(connection, run_id, kind, at, data)

    monkeypatch.setattr(tool_env.runs, "_append_event", fail)
    with pytest.raises(RuntimeError):
        calls.start_call(run.id, run.user_id, "call-1", BudgetCharge(tool_calls=1))
    assert calls.get_call(run.id, run.user_id, "call-1").status == "pending"
    assert tool_env.runs.get_run(run.id, run.user_id).budget_usage.tool_calls == 0


def test_result_and_event_are_rolled_back_together(tool_env, monkeypatch) -> None:
    run, calls = tool_env.run, tool_env.calls
    calls.prepare_calls(run.id, run.user_id, [invocation()])
    calls.start_call(run.id, run.user_id, "call-1", BudgetCharge(tool_calls=1))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated event failure")

    monkeypatch.setattr(tool_env.runs, "_append_event", fail)
    with pytest.raises(RuntimeError):
        calls.finish_call(run.id, run.user_id, "call-1", ToolResult.success())
    assert calls.get_call(run.id, run.user_id, "call-1").status == "executing"
    assert calls.get_call(run.id, run.user_id, "call-1").result is None


def test_migration_keeps_v1_run_and_events(tmp_path) -> None:
    from app.config import Settings
    from app.storage.models import RunRequest, SubscriptionSnapshot
    from app.storage.runs import RunStore

    settings = Settings(_env_file=None, data_dir=tmp_path)
    database = Database(settings.database_path)
    with database.connection() as connection:
        for statement in SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
    runs = RunStore(database, settings)
    run = runs.create_run(
        RunRequest(
            user_id="old-user",
            subscription_id="old-sub",
            subscription=SubscriptionSnapshot(
                nickname="原用户", email="old@example.com", topics=("AI",)
            ),
        )
    )
    database.initialize()
    database.initialize()
    assert runs.get_run(run.id, run.user_id) == run
    assert len(runs.list_events(run.id, run.user_id)) == 1
    assert CallStore(runs).prepare_calls(run.id, run.user_id, [invocation()])[0].status == "pending"


def test_migration_failure_keeps_old_schema_version(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "news.sqlite3")
    with database.connection() as connection:
        for statement in SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
    monkeypatch.setattr(
        database_module, "SCHEMA_V2", ("CREATE TABLE scratch_test (id INTEGER)", "INVALID")
    )
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='scratch_test'"
            ).fetchone()
            is None
        )
