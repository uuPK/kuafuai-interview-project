import json
import logging

import pytest
from pydantic import ValidationError

from app.harness.trace import EventProjection, argument_summary, result_summary
from app.storage.timeline import TimelineStore
from app.tools.models import ToolFailure, ToolResult


def test_timeline_is_owner_checked_paginated_and_reports_stop(tool_env):
    env = tool_env
    store = TimelineStore(env.runs)
    first = store.page(env.run.id, "user-1", limit=1)
    assert first.has_more and first.next_after_id == first.events[-1].id
    second = store.page(env.run.id, "user-1", after_id=first.next_after_id, limit=1)
    assert not second.has_more and second.events[0].id > first.events[-1].id
    empty = store.page(env.run.id, "user-1", after_id=second.next_after_id)
    assert not empty.events and empty.next_after_id == second.next_after_id
    for user, run in [("other", env.run.id), ("user-1", "missing")]:
        with pytest.raises(LookupError):
            store.page(run, user)
    from app.storage.models import StateChange

    env.runs.transition(env.run.id, "user-1", StateChange(status="failed", stop_reason="test_stop"))
    final = store.page(env.run.id, "user-1")
    assert final.status == "failed" and final.stop_reason == "test_stop"
    assert final.events[-1].reason_basis == "missing"


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": "2"},
        {"after_id": -1},
        {"after_id": True},
    ],
)
def test_timeline_rejects_invalid_cursor_and_limits(tool_env, query):
    with pytest.raises(ValidationError):
        TimelineStore(tool_env.runs).page(tool_env.run.id, "user-1", **query)


def test_events_redact_and_drop_raw_protocol_before_projection(tool_env, caplog):
    env = tool_env
    caplog.set_level(logging.INFO, logger="app.events")
    with env.database.transaction() as connection:
        env.runs._append_event(
            connection,
            env.run.id,
            "custom",
            env.runs.now(),
            {
                "parameters": {
                    "query": "fixture-deep-key password=abc reader@example.com",
                    "token": "bad",
                },
                "reasoning_content": "PRIVATE_REASONING",
                "body": "PRIVATE_BODY",
                "raw": {"content": "PRIVATE_PROTOCOL"},
                "recipient": "reader@example.com",
                "authorization": "Bearer unknown-secret",
                "source_id": "src-reference",
                "error_code": "TEST_FAILURE",
            },
        )
    page = TimelineStore(env.runs).page(env.run.id, "user-1")
    serialized = page.model_dump_json() + " ".join(r.message for r in caplog.records)
    for text in (
        "fixture-deep-key",
        "password=abc",
        "reader@example.com",
        "PRIVATE_",
        "unknown-secret",
    ):
        assert text not in serialized
    assert page.events[-1].data["source_id"] == "src-reference"
    assert page.events[-1].reason_basis == "saved_feedback"
    with env.database.connection() as connection:
        data = connection.execute(
            "SELECT data_json FROM run_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "fixture-deep-key" not in data and "unknown-secret" not in data


def test_logs_publish_only_committed_events_outside_write_lock(tool_env, caplog):
    env = tool_env
    caplog.set_level(logging.INFO, logger="app.events")
    emitted, original = [], env.database.event_sink

    def sink(row):
        with env.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            assert connection.execute(
                "SELECT 1 FROM run_events WHERE id=?", (row["id"],)
            ).fetchone()
            connection.execute("ROLLBACK")
        emitted.append(row["kind"])
        original(row)

    env.database.event_sink = sink
    with pytest.raises(RuntimeError), env.database.transaction() as connection:
        env.runs._append_event(connection, env.run.id, "rolled_back", env.runs.now(), {})
        raise RuntimeError("fixture rollback")
    assert not emitted
    with env.database.transaction() as connection:
        env.runs._append_event(connection, env.run.id, "committed", env.runs.now(), {})
    assert emitted == ["committed"]
    record = json.loads(next(r.message for r in caplog.records if r.name == "app.events"))
    assert record["event"] == "committed" and record["run_id"] == env.run.id
    assert type(record["event_id"]) is int and record["occurred_at"]


def test_broken_logging_cannot_undo_delivery_state(tool_env):
    env = tool_env

    def broken(row):
        raise RuntimeError("fixture log failure")

    env.database.event_sink = broken
    with env.database.transaction() as connection:
        env.runs._append_event(connection, env.run.id, "committed", env.runs.now(), {})
    assert env.runs.list_events(env.run.id, "user-1")[-1].kind == "committed"


def test_call_events_show_safe_parameters_and_duration(agent_env, response_factory):
    env = agent_env
    response = response_factory(
        ("write", "write_file", {"path": "scratch/a", "content": "SECRET_NOTE"}),
        reasoning="PRIVATE_REASONING",
        response_id="provider-1",
        usage={"total_tokens": 12},
    )
    env.conversations.save_response(env.run.id, "user-1", response)
    call = env.calls.get_call(env.run.id, "user-1", "write")
    from app.storage.models import BudgetCharge

    env.calls.start_call(env.run.id, "user-1", "write", BudgetCharge(tool_calls=1))
    env.calls.finish_call(
        env.run.id, "user-1", "write", ToolResult.success({"body": "PRIVATE_BODY"})
    )
    page = TimelineStore(env.runs).page(env.run.id, "user-1")
    pending = next(event for event in page.events if event.kind == "tool_pending")
    assert pending.data["parameters"] == {"path": "scratch/a", "content_chars": 11}
    done = next(event for event in page.events if event.kind == "tool_completed")
    assert done.data["elapsed_ms"] >= 0 and done.data["tool_call_id"] == call.tool_call_id
    saved = next(event for event in page.events if event.kind == "model_response_saved")
    assert saved.data["response_id"] == "provider-1" and saved.data["usage"]["total_tokens"] == 12
    assert "SECRET_NOTE" not in page.model_dump_json() and "PRIVATE_" not in page.model_dump_json()
    assert env.conversations.responses(env.run.id, "user-1")[0] == response


def test_projection_bounds_unknown_fields_large_content_and_nesting(tool_env):
    projection = EventProjection(tool_env.settings)
    assert argument_summary("unknown", '{"body":"secret"}') == {}
    assert argument_summary("x", "broken") == {"parameters_omitted": True}
    assert argument_summary("x", "[]") == {"parameters_omitted": True}
    assert result_summary(ToolResult.success(["body"])) == {}
    assert result_summary(ToolResult.failure("TEST", "failure")) == {"issues": []}
    assert projection.data({"query": "x" * 1000})["query"].endswith("…")
    assert len(projection.data({"items": [1] * 100})["items"]) == 20
    assert projection.data([]) == {}
    assert projection.data({"limit": float("inf")})["limit"] == "[OMITTED]"
    nested = {"items": {"items": {"items": {"items": {"items": {"items": [1]}}}}}}
    assert "OMITTED" in json.dumps(projection.data(nested))
    large = {"query": "x" * 500, "items": ["x" * 500] * 20, "issues": ["x" * 500] * 20}
    assert projection.data(large) == {"parameters_omitted": True}


def test_corrupt_event_returns_a_controlled_error(tool_env):
    env = tool_env
    with env.database.connection() as connection:
        connection.execute("UPDATE run_events SET data_json='broken'")
    with pytest.raises(ToolFailure) as error:
        TimelineStore(env.runs).page(env.run.id, "user-1")
    assert error.value.result.error.code == "TIMELINE_STATE_PROBLEM"
