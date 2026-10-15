import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import app.storage.database as database_module
from app.agent.brief_rules import BriefRules
from app.agent.briefs import ReviewOutcome
from app.agent.prompts import initial_messages
from app.agent.stubs import ScriptedModel
from app.storage.briefs import BriefStore
from app.storage.conversations import ConversationStore
from app.storage.outbox import OutboxStore
from app.storage.timeline import TimelineStore
from app.tools.executor import ToolExecutor
from app.tools.models import ToolFailure, ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.submit import register_submission
from tests.research_support import ResearchWorld
from tests.test_briefs import brief_env as _brief_env
from tests.test_briefs import counts, empty_draft, execute, invocation, recover_result
from tests.test_recovery import attach, make_worker

brief_env = _brief_env


def scheduled(ready, *, enabled=True, email=None):
    env = ready.env
    subscription = env.runs.get_run(env.run.id, "user-1").subscription.model_dump()
    subscription["enabled"] = enabled
    if email is not None:
        subscription["email"] = email
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET mode='scheduled',scheduled_for=window_end,subscription_json=? "
            "WHERE id=?",
            (json.dumps(subscription, ensure_ascii=False), env.run.id),
        )


def mail_count(env):
    with env.database.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0]


def pairing(ready, response_factory):
    env = ready.env
    conversations = ConversationStore(env.calls)
    conversations.initialize(env.run.id, "user-1", initial_messages(env.run))
    conversations.save_response(
        env.run.id, "user-1", response_factory(("submit", "submit_brief", {"draft": ready.draft}))
    )
    return conversations


def test_scheduled_brief_freezes_message_and_is_unique(brief_env, response_factory):
    ready, env = brief_env, brief_env.env
    scheduled(ready)
    conversations = pairing(ready, response_factory)
    result = execute(
        ready, on_finished=lambda conn, call: conversations.record_feedback(conn, "user-1", call)
    )
    record = ready.store.outbox.get(result.data["brief_id"], "user-1")
    assert record.status == "pending" and record.attempts == 0
    assert record.recipient == "reader@example.com"
    assert ready.draft["items"][0]["summary"] in record.body_text
    assert ready.meta.url in record.body_text and "新闻窗口" in record.body_text
    assert "\r" not in record.subject and "\n" not in record.subject
    assert record.recipient not in repr(record) and record.body_text not in repr(record)
    assert execute(ready) == result and len(ready.reviews) == 1
    with env.database.transaction() as connection:
        assert (
            ready.store.outbox.reserve(connection, env.run.id, "user-1", record.brief_id) == record
        )
    assert mail_count(env) == 1 and counts(ready) == (1, 1, 1)
    assert len([m for m in conversations.messages(env.run.id, "user-1") if m.role == "tool"]) == 1
    events = env.runs.list_events(env.run.id, "user-1")
    assert len([event for event in events if event.kind == "mail_queued"]) == 1
    assert record.recipient not in " ".join(event.model_dump_json() for event in events)
    with pytest.raises(LookupError):
        ready.store.outbox.get(record.brief_id, "other")
    with pytest.raises(LookupError):
        ready.store.outbox.get("missing", "user-1")
    env.database.initialize()
    assert OutboxStore(env.runs).get(record.brief_id, "user-1") == record


@pytest.mark.parametrize("scheduled_mode,enabled", [(False, True), (True, False)])
def test_preview_or_disabled_subscription_saves_without_mail(brief_env, scheduled_mode, enabled):
    ready, env = brief_env, brief_env.env
    if scheduled_mode:
        scheduled(ready, enabled=enabled)
    assert execute(ready).ok and counts(ready) == (1, 1, 1) and mail_count(env) == 0
    kinds = [event.kind for event in env.runs.list_events(env.run.id, "user-1")]
    assert ("mail_suppressed" in kinds) is scheduled_mode


def test_live_subscription_seam_uses_the_same_transaction(brief_env):
    ready, env = brief_env, brief_env.env
    scheduled(ready)
    observed = []

    def disabled(connection, run):
        observed.append((connection.in_transaction, run.id, run.status))
        assert connection.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 1
        return False

    ready.store.outbox.delivery_enabled = disabled
    assert execute(ready).ok and mail_count(env) == 0
    assert observed == [(True, env.run.id, "completed")]


@pytest.mark.parametrize("point", ["before_queue", "after_queue", "feedback"])
def test_delivery_rollback_and_same_intent_recovery(
    brief_env, response_factory, monkeypatch, caplog, point
):
    ready, env = brief_env, brief_env.env
    scheduled(ready)
    conversations = pairing(ready, response_factory)
    reserve = ready.store.outbox.reserve

    def interrupted(connection, *args):
        if point == "after_queue":
            reserve(connection, *args)
        raise RuntimeError("fixture delivery interrupted")

    def feedback(connection, call):
        conversations.record_feedback(connection, "user-1", call)
        if point == "feedback":
            raise RuntimeError("fixture delivery interrupted")

    caplog.set_level(logging.INFO, logger="app.events")
    if point != "feedback":
        monkeypatch.setattr(ready.store.outbox, "reserve", interrupted)
    with pytest.raises(RuntimeError):
        execute(ready, on_finished=feedback)
    assert counts(ready) == (0, 0, 1) and mail_count(env) == 0
    assert env.runs.get_run(env.run.id, "user-1").status == "running"
    assert not [m for m in conversations.messages(env.run.id, "user-1") if m.role == "tool"]
    assert not any(
        json.loads(r.message)["event"] in {"mail_queued", "brief_saved"}
        for r in caplog.records
        if r.name == "app.events"
    )
    monkeypatch.setattr(ready.store.outbox, "reserve", reserve)
    result = recover_result(ready)
    ready.executor.finish(
        env.run.id,
        "user-1",
        invocation(ready.draft),
        result,
        on_finished=lambda conn, call: conversations.record_feedback(conn, "user-1", call),
    )
    assert counts(ready) == (1, 1, 1) and mail_count(env) == 1
    assert len(ready.reviews) == 1 and execute(ready) == result


@pytest.mark.parametrize(
    "email",
    [
        "reader@example.com\r\nBcc: other@example.com",
        "a@b.com,other@b.com",
        "Name <a@b.com>",
        ".a@example.com",
        "a..b@example.com",
        "a@-bad.com",
    ],
)
def test_invalid_server_recipient_rolls_back_delivery(brief_env, email):
    ready = brief_env
    scheduled(ready, email=email)
    with pytest.raises(ToolFailure) as error:
        execute(ready)
    assert error.value.result.error.code == "MAIL_RECIPIENT_INVALID"
    assert counts(ready) == (0, 0, 1) and mail_count(ready.env) == 0


@pytest.mark.parametrize(
    "column,value",
    [
        ("body_text", "changed"),
        ("recipient", "other@example.com"),
        ("status", "invalid"),
    ],
)
def test_outbox_integrity_checks_and_sql_constraints(brief_env, column, value):
    ready, env = brief_env, brief_env.env
    scheduled(ready)
    result = execute(ready)
    if column == "status":
        with pytest.raises(sqlite3.IntegrityError), env.database.transaction() as connection:
            connection.execute("UPDATE mail_outbox SET status=?", (value,))
    else:
        with env.database.transaction() as connection:
            connection.execute(f"UPDATE mail_outbox SET {column}=?", (value,))
        with pytest.raises(ToolFailure) as error:
            ready.store.outbox.get(result.data["brief_id"], "user-1")
        assert error.value.result.error.code == "MAIL_STATE_PROBLEM"


def test_reservation_requires_owner_saved_brief_and_transaction(brief_env):
    ready, env = brief_env, brief_env.env
    with env.database.connection() as connection, pytest.raises(ValueError):
        ready.store.outbox.reserve(connection, env.run.id, "user-1", "missing")
    with env.database.transaction() as connection, pytest.raises(PermissionError):
        ready.store.outbox.reserve(connection, env.run.id, "user-1", "missing")
    with env.database.transaction() as connection, pytest.raises(LookupError):
        ready.store.outbox.reserve(connection, env.run.id, "other", "missing")


def test_worker_recovers_scheduled_submission_without_duplicate_queue(brief_env, response_factory):
    async def run():
        ready, env = brief_env, brief_env.env
        scheduled(ready)
        conversations = pairing(ready, response_factory)

        # Save the checked intent but interrupt the final atomic delivery.
        def interrupted(*args):
            raise RuntimeError("fixture crash")

        with pytest.raises(RuntimeError):
            await ready.executor.execute(
                env.run.id, "user-1", invocation(ready.draft), on_finished=interrupted
            )
        assert mail_count(env) == 0
        attach(env, ready.executor)
        model = ScriptedModel([])
        done = await make_worker(env, model, sources=ready.fetched.store).run_once()
        assert done.status == "completed" and not model.requests
        assert mail_count(env) == 1 and counts(ready) == (1, 1, 1)
        assert len(ready.reviews) == 1
        assert (
            len([m for m in conversations.messages(env.run.id, "user-1") if m.role == "tool"]) == 1
        )
        page = TimelineStore(env.runs).page(env.run.id, "user-1", limit=100)
        assert {"worker_reclaimed", "tool_call_recovered", "mail_queued", "brief_saved"} <= {
            event.kind for event in page.events
        }

    asyncio.run(run())


def test_v12_migration_is_atomic_and_preserves_existing_brief(brief_env, monkeypatch):
    ready, env = brief_env, brief_env.env
    result = execute(ready)
    before = ready.store.get(result.data["brief_id"], "user-1")
    with env.database.connection() as connection:
        connection.execute("DROP TABLE mail_outbox")
        for table in (
            "mail_attempts",
            "mail_dispatch",
            "schedule_notices",
            "subscription_schedules",
            "scheduled_runs",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE subscriptions")
        connection.execute("DROP TABLE auth_sessions")
        connection.execute("DROP TABLE auth_users")
        connection.execute("DROP TABLE auth_attempts")
        connection.execute("PRAGMA user_version=11")
    schema = database_module.SCHEMA_V12
    monkeypatch.setattr(database_module, "SCHEMA_V12", (*schema, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        env.database.initialize()
    with env.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='mail_outbox'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V12", schema)
    env.database.initialize()
    env.database.initialize()
    assert ready.store.get(before["brief_id"], "user-1") == before and mail_count(env) == 0


def test_submission_rejection_never_queues(brief_env):
    scheduled(brief_env)
    brief_env.draft["items"][0]["citations"][0]["quote"] = "wrong"
    assert not execute(brief_env).ok and mail_count(brief_env.env) == 0


def test_valid_no_news_scheduled_brief_has_fixed_message(tool_env):
    async def run():
        env = tool_env
        scheduled(SimpleNamespace(env=env))
        async with ResearchWorld(env, zero_first=True) as world:
            await world.executor.execute(
                env.run.id,
                "user-1",
                ToolInvocation(
                    tool_call_id="feed",
                    name="read_feed",
                    arguments='{"feed_id":"fixture-official"}',
                ),
            )
            store = BriefStore(world.research, BriefRules(world.sources, env.settings))
            registry = ToolRegistry()

            async def reviewer(*args):
                return ReviewOutcome(approved=True, issues=())

            register_submission(registry, store, reviewer=reviewer, review_basis="fixture")
            result = await ToolExecutor(registry, env.calls, env.settings).execute(
                env.run.id, "user-1", invocation(empty_draft())
            )
            record = store.outbox.get(result.data["brief_id"], "user-1")
            assert "暂无符合订阅条件的新闻" in record.body_text
            assert "未覆盖话题" in record.body_text and record.status == "pending"
            assert env.runs.get_run(env.run.id, "user-1").status == "no_news"
            with env.database.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM delivered_history").fetchone()[0] == 0
                )

    asyncio.run(run())


def test_concurrent_repeat_reservations_keep_one_record(brief_env):
    ready, env = brief_env, brief_env.env
    scheduled(ready)
    result = execute(ready)

    def reserve(_):
        with env.database.transaction() as connection:
            return ready.store.outbox.reserve(
                connection, env.run.id, "user-1", result.data["brief_id"]
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(reserve, (1, 2)))
    assert records[0] == records[1] and mail_count(env) == 1


def test_delivery_policy_must_be_explicit_boolean(brief_env):
    scheduled(brief_env)
    brief_env.store.outbox.delivery_enabled = lambda connection, run: "unknown"
    with pytest.raises(TypeError):
        execute(brief_env)
    assert counts(brief_env) == (0, 0, 1) and mail_count(brief_env.env) == 0
