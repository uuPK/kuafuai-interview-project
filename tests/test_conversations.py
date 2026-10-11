import asyncio
import sqlite3

import pytest

import app.storage.database as database_module
from app.agent.prompts import initial_messages
from app.agent.protocol import InputMessage, ToolMessage
from app.storage.calls import CallStatus, CallStore
from app.storage.conversations import ConversationConflictError, ConversationStore
from app.storage.database import Database
from app.storage.models import StateChange
from app.storage.runs import InvalidRunStateError, RunNotFoundError, RunStore
from app.storage.schema import SCHEMA_V1, SCHEMA_V2
from app.tools.models import ToolInvocation, ToolResult


def test_initialization_is_idempotent_and_validates_roles(agent_env) -> None:
    env = agent_env
    before = env.conversations.messages(env.run.id, "user-1")
    env.conversations.initialize(env.run.id, "user-1", initial_messages(env.run))
    assert env.conversations.messages(env.run.id, "user-1") == before
    with pytest.raises(ValueError):
        env.conversations.initialize(
            env.run.id, "user-1", (InputMessage(role="user", content="x"),) * 2
        )


def test_response_and_all_calls_are_saved_before_execution(agent_env, response_factory) -> None:
    env = agent_env
    response = response_factory(
        ("first", "web_search", {"query": "AI"}),
        ("second", "fetch_url", {"candidate_id": "fixture-model"}),
        reasoning="controlled protocol",
        raw={"request_id": "original"},
    )
    assert env.conversations.save_response(env.run.id, "user-1", response) == 1
    assert env.conversations.responses(env.run.id, "user-1") == (response,)
    assert env.conversations.messages(env.run.id, "user-1")[-1] == response.message
    assert [
        call.tool_call_id for call in env.conversations.pending_calls(env.run.id, "user-1")
    ] == ["first", "second"]
    assert env.calls.get_call(env.run.id, "user-1", "second").status == "pending"
    assert env.runs.get_run(env.run.id, "user-1").budget_usage.tool_calls == 0


def test_response_transaction_rolls_back_calls_and_message(
    agent_env, response_factory, monkeypatch
):
    env = agent_env
    append = env.runs._append_event

    def fail(connection, run_id, kind, at, data):
        if kind == "model_response_saved":
            raise RuntimeError("simulated save interruption")
        append(connection, run_id, kind, at, data)

    monkeypatch.setattr(env.runs, "_append_event", fail)
    with pytest.raises(RuntimeError):
        env.conversations.save_response(
            env.run.id, "user-1", response_factory(("search", "web_search", {"query": "AI"}))
        )
    assert env.conversations.responses(env.run.id, "user-1") == ()
    assert len(env.conversations.messages(env.run.id, "user-1")) == 2
    with pytest.raises(LookupError):
        env.calls.get_call(env.run.id, "user-1", "search")


def test_new_response_requires_all_feedback_and_new_ids(agent_env, response_factory) -> None:
    env = agent_env
    response = response_factory(("same", "web_search", {"query": "AI"}))
    env.conversations.save_response(env.run.id, "user-1", response)
    with pytest.raises(ConversationConflictError, match="feedback"):
        env.conversations.save_response(env.run.id, "user-1", response_factory(content="next"))
    env.calls.finish_call(env.run.id, "user-1", "same", ToolResult.failure("REJECTED", "测试"))
    env.conversations.pair_saved_result(env.run.id, "user-1", "same")
    with pytest.raises(ConversationConflictError, match="new tool call IDs"):
        env.conversations.save_response(env.run.id, "user-1", response)
    assert len(env.conversations.responses(env.run.id, "user-1")) == 1


def test_feedback_is_ordered_idempotent_and_cannot_be_fabricated(
    agent_env, response_factory
) -> None:
    env = agent_env
    env.conversations.save_response(
        env.run.id, "user-1", response_factory(("a", "unknown", {}), ("b", "unknown", {}))
    )
    with pytest.raises(ConversationConflictError, match="durable"):
        env.conversations.pair_saved_result(env.run.id, "user-1", "a")
    for call_id in ("a", "b"):
        env.calls.finish_call(
            env.run.id, "user-1", call_id, ToolResult.failure("UNKNOWN_TOOL", "x")
        )
    with pytest.raises(ConversationConflictError, match="order"):
        env.conversations.pair_saved_result(env.run.id, "user-1", "b")
    env.conversations.pair_saved_result(env.run.id, "user-1", "a")
    env.conversations.pair_saved_result(env.run.id, "user-1", "a")
    env.conversations.pair_saved_result(env.run.id, "user-1", "b")
    feedback = [
        m for m in env.conversations.messages(env.run.id, "user-1") if isinstance(m, ToolMessage)
    ]
    assert [m.tool_call_id for m in feedback] == ["a", "b"]
    assert env.conversations.pending_calls(env.run.id, "user-1") == ()
    with pytest.raises(ConversationConflictError, match="cannot be changed"):
        with env.database.transaction() as connection:
            connection.execute(
                "UPDATE run_messages SET message_json = '{}' WHERE tool_call_id = 'a'"
            )
            env.conversations.record_feedback(
                connection, "user-1", env.calls.get_call(env.run.id, "user-1", "a")
            )


def test_submission_result_feedback_and_state_roll_back_together(
    agent_env, response_factory, monkeypatch
):
    env = agent_env
    env.conversations.save_response(
        env.run.id,
        "user-1",
        response_factory(("submit", "submit_brief", {"draft": {"title": "测试"}})),
    )
    append = env.runs._append_event

    def fail(connection, run_id, kind, at, data):
        if kind == "tool_feedback_saved":
            raise RuntimeError("simulated feedback interruption")
        append(connection, run_id, kind, at, data)

    monkeypatch.setattr(env.runs, "_append_event", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(env.runner(None)._finish_pending(env.run.id, "user-1"))
    assert env.calls.get_call(env.run.id, "user-1", "submit").status == "executing"
    assert env.calls.get_call(env.run.id, "user-1", "submit").result is None
    assert env.runs.get_run(env.run.id, "user-1").status == "running"
    assert len(env.conversations.messages(env.run.id, "user-1")) == 3


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_failed_results_cannot_complete_a_run(agent_env, response_factory, status) -> None:
    env = agent_env
    env.conversations.save_response(env.run.id, "user-1", response_factory(("a", "unknown", {})))
    env.calls.finish_call(env.run.id, "user-1", "a", ToolResult.failure("UNKNOWN_TOOL", "x"))
    with pytest.raises(ConversationConflictError):
        env.conversations.pair_saved_result(env.run.id, "user-1", "a", completion_status=status)
    assert env.runs.get_run(env.run.id, "user-1").status == "running"


@pytest.mark.parametrize(
    "operation", ["initialize", "messages", "responses", "save", "pending", "pair", "check"]
)
def test_conversations_check_owner_on_every_operation(
    agent_env, response_factory, operation
) -> None:
    env = agent_env
    actions = {
        "initialize": lambda: env.conversations.initialize(
            env.run.id, "other", initial_messages(env.run)
        ),
        "messages": lambda: env.conversations.messages(env.run.id, "other"),
        "responses": lambda: env.conversations.responses(env.run.id, "other"),
        "save": lambda: env.conversations.save_response(env.run.id, "other", response_factory()),
        "pending": lambda: env.conversations.pending_calls(env.run.id, "other"),
        "pair": lambda: env.conversations.pair_saved_result(env.run.id, "other", "unknown"),
        "check": lambda: env.conversations.check_submission(env.run.id, "other"),
    }
    with pytest.raises(RunNotFoundError):
        actions[operation]()


def test_missing_submission_reminder_is_persistent_and_sent_only_once(agent_env, response_factory):
    env = agent_env
    env.conversations.save_response(env.run.id, "user-1", response_factory(content="已完成"))
    env.conversations.check_submission(env.run.id, "user-1")
    env.conversations.check_submission(env.run.id, "user-1")
    assert len(env.conversations.messages(env.run.id, "user-1")) == 4
    reopened = ConversationStore(CallStore(RunStore(Database(env.database.path), env.settings)))
    reopened.save_response(env.run.id, "user-1", response_factory(content="仍未提交"))
    failed = reopened.check_submission(env.run.id, "user-1")
    assert failed.status == "failed" and failed.stop_reason == "missing_submission"
    assert len(reopened.messages(env.run.id, "user-1")) == 5


def test_uninitialized_and_terminal_conversation_guards(tool_env, response_factory) -> None:
    env = tool_env
    store = ConversationStore(env.calls)
    assert store.check_submission(env.run.id, "user-1").status == "running"
    with pytest.raises(ConversationConflictError, match="initial"):
        store.save_response(env.run.id, "user-1", response_factory())
    env.runs.transition(env.run.id, "user-1", StateChange(status="completed"))
    with pytest.raises(InvalidRunStateError):
        store.initialize(env.run.id, "user-1", initial_messages(env.run))
    with pytest.raises(InvalidRunStateError):
        store.save_response(env.run.id, "user-1", response_factory())


def test_v2_migration_preserves_existing_calls_and_is_idempotent(tmp_path, tool_env):
    database = Database(tmp_path / "old.sqlite3")
    with database.connection() as connection:
        for statement in (*SCHEMA_V1, *SCHEMA_V2):
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
    runs = RunStore(database, tool_env.settings)
    request = tool_env.run
    from app.storage.models import RunRequest

    run = runs.create_run(
        RunRequest(
            user_id=request.user_id,
            subscription_id=request.subscription_id,
            subscription=request.subscription,
        )
    )
    calls = CallStore(runs)
    invocation = ToolInvocation(tool_call_id="old", name="web_search", arguments="{}")
    old = calls.prepare_calls(run.id, run.user_id, [invocation])[0]
    database.initialize()
    database.initialize()
    assert calls.get_call(run.id, run.user_id, "old") == old
    assert ConversationStore(calls).messages(run.id, run.user_id) == ()


def test_v3_migration_failure_rolls_back(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "old.sqlite3")
    with database.connection() as connection:
        for statement in (*SCHEMA_V1, *SCHEMA_V2):
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
    monkeypatch.setattr(
        database_module, "SCHEMA_V3", ("CREATE TABLE partial (id INTEGER)", "INVALID")
    )
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone()
            is None
        )


def test_feedback_checks_saved_result_not_just_caller_record(agent_env, response_factory) -> None:
    env = agent_env
    env.conversations.save_response(
        env.run.id, "user-1", response_factory(("a", "web_search", {"query": "AI"}))
    )
    pending = env.calls.get_call(env.run.id, "user-1", "a")
    fabricated = pending.model_copy(
        update={"status": CallStatus.COMPLETED, "result": ToolResult.success()}
    )
    with env.database.transaction() as connection:
        with pytest.raises(ConversationConflictError, match="durable"):
            env.conversations.record_feedback(connection, "user-1", fabricated)
    assert env.calls.get_call(env.run.id, "user-1", "a").status == "pending"
    assert len(env.conversations.messages(env.run.id, "user-1")) == 3


def test_run_foreign_keys_cascade_without_orphan_protocol_records(
    agent_env, response_factory
) -> None:
    env = agent_env
    env.conversations.save_response(
        env.run.id, "user-1", response_factory(("a", "web_search", {"query": "AI"}))
    )
    env.calls.finish_call(env.run.id, "user-1", "a", ToolResult.failure("TEST_STOP", "测试"))
    env.conversations.pair_saved_result(env.run.id, "user-1", "a")
    with env.database.transaction() as connection:
        connection.execute("DELETE FROM runs WHERE id = ?", (env.run.id,))
        for table in ("run_messages", "model_responses", "model_tool_calls", "tool_calls"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
