import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.storage.database import Database
from app.storage.models import (
    BudgetCharge,
    BudgetUsage,
    RunMode,
    RunRequest,
    StateChange,
    SubscriptionSnapshot,
)
from app.storage.runs import BudgetExceededError, InvalidRunStateError, RunNotFoundError, RunStore

CREATED = datetime(2026, 10, 11, 1, tzinfo=UTC)
STARTED = CREATED + timedelta(minutes=1)


def subscription(**overrides) -> SubscriptionSnapshot:
    return SubscriptionSnapshot(
        **{
            "nickname": "研究员",
            "email": "reader@example.com",
            "topics": ["开源模型", "AI 芯片"],
            "keywords": ["推理"],
            "exclude_keywords": ["广告"],
            **overrides,
        }
    )


def request(**overrides) -> RunRequest:
    return RunRequest(
        **{
            "user_id": "user-1",
            "subscription_id": "sub-1",
            "subscription": subscription(),
            **overrides,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    return RunStore(database, settings)


def start_run(store: RunStore, **overrides):
    run = store.create_run(request(**overrides), now=CREATED)
    return store.transition(run.id, run.user_id, StateChange(status="running"), now=STARTED)


def test_preview_saves_snapshot_and_fixed_utc_window(store: RunStore) -> None:
    local = CREATED.astimezone(timezone(timedelta(hours=8)))
    run = store.create_run(request(), now=local)
    assert run.status == "queued"
    assert run.created_at == CREATED
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.window_end == CREATED
    assert run.window_start == CREATED - timedelta(hours=24)
    assert run.scheduled_for is None
    assert run.subscription == subscription()
    assert run.budget_usage == BudgetUsage()
    assert run.started_at is run.deadline_at is None
    events = store.list_events(run.id, "user-1")
    assert [event.kind for event in events] == ["run_created"]
    assert events[0].data == {"mode": "preview"}


def test_scheduled_window_uses_planned_time_even_when_created_late(store: RunStore) -> None:
    run = store.create_run(request(mode="scheduled", scheduled_for=CREATED), now=STARTED)
    assert run.mode == RunMode.SCHEDULED
    assert run.created_at == STARTED
    assert run.scheduled_for == CREATED
    assert run.window_end == CREATED
    assert run.window_start == CREATED - timedelta(hours=24)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "scheduled"},
        {"scheduled_for": CREATED},
        {"mode": "scheduled", "scheduled_for": datetime(2026, 10, 11, 9)},
        {"user_id": " "},
        {"subscription_id": ""},
        {"unexpected": "value"},
    ],
)
def test_invalid_run_request_is_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"topics": []},
        {"topics": [" "]},
        {"max_items": 0},
        {"max_items": 6},
        {"push_time": "24:00"},
        {"push_time": "09:60"},
        {"email": ""},
        {"topics": ["AI"] * 21},
        {"keywords": ["AI"] * 101},
    ],
)
def test_invalid_snapshot_is_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        subscription(**overrides)


def test_snapshot_is_detached_from_user_edits_and_cannot_be_mutated(store: RunStore) -> None:
    input_topics = ["开源模型"]
    original = subscription(topics=input_topics)
    run = store.create_run(request(subscription=original), now=CREATED)
    input_topics.append("AI 芯片")
    revised = original.model_copy(update={"topics": ("AI 芯片",)})
    assert revised.topics == ("AI 芯片",)
    assert store.get_run(run.id, "user-1").subscription.topics == ("开源模型",)
    with pytest.raises(ValidationError):
        run.subscription.max_items = 1


def test_naive_operation_time_is_rejected_without_writing(store: RunStore) -> None:
    with pytest.raises(ValueError, match="timezone"):
        store.create_run(request(), now=datetime(2026, 10, 11))
    with store.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_first_start_sets_deadline_and_retry_preserves_it(store: RunStore) -> None:
    run = start_run(store)
    assert run.started_at == STARTED
    assert run.deadline_at == STARTED + timedelta(seconds=600)
    used = store.consume_budget(run.id, "user-1", BudgetCharge(model_requests=1), now=STARTED)
    retry = STARTED + timedelta(seconds=30)
    waiting = store.transition(
        run.id, "user-1", StateChange(status="waiting_retry", retry_at=retry), now=STARTED
    )
    assert waiting.retry_at == retry
    with pytest.raises(InvalidRunStateError, match="not due"):
        store.transition(run.id, "user-1", StateChange(status="running"), now=STARTED)
    resumed = store.transition(run.id, "user-1", StateChange(status="running"), now=retry)
    assert resumed.retry_at is None
    assert resumed.started_at == run.started_at
    assert resumed.deadline_at == run.deadline_at
    assert resumed.window_end == run.window_end
    assert resumed.budget_usage == used.budget_usage


@pytest.mark.parametrize("status", ["completed", "no_news", "failed"])
def test_terminal_states_cannot_restart_or_consume_budget(store: RunStore, status: str) -> None:
    run = start_run(store)
    change = StateChange(
        status=status, stop_reason="provider_auth_failed" if status == "failed" else None
    )
    ended = store.transition(run.id, "user-1", change, now=STARTED)
    assert ended.status == status
    assert ended.stop_reason == change.stop_reason
    with pytest.raises(InvalidRunStateError):
        store.transition(run.id, "user-1", StateChange(status="running"), now=STARTED)
    with pytest.raises(InvalidRunStateError):
        store.consume_budget(run.id, "user-1", BudgetCharge(tool_calls=1), now=STARTED)


def test_configuration_failure_can_stop_a_queued_run(store: RunStore) -> None:
    run = store.create_run(request(), now=CREATED)
    failed = store.transition(
        run.id, "user-1", StateChange(status="failed", stop_reason="missing_config"), now=CREATED
    )
    assert failed.started_at is failed.deadline_at is None
    assert failed.stop_reason == "missing_config"


@pytest.mark.parametrize("status", ["queued", "waiting_retry", "completed", "no_news"])
def test_invalid_transitions_from_queued_do_not_write_events(store: RunStore, status: str) -> None:
    run = store.create_run(request(), now=CREATED)
    change = StateChange(status=status, retry_at=STARTED if status == "waiting_retry" else None)
    with pytest.raises(InvalidRunStateError):
        store.transition(run.id, "user-1", change, now=CREATED)
    assert store.get_run(run.id, "user-1") == run
    assert len(store.list_events(run.id, "user-1")) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "waiting_retry"},
        {"status": "running", "retry_at": CREATED},
        {"status": "failed"},
        {"status": "failed", "stop_reason": "secret: raw input"},
        {"status": "completed", "stop_reason": "error"},
    ],
)
def test_state_change_requires_correct_details(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        StateChange(**overrides)


def test_retry_time_must_be_in_the_future(store: RunStore) -> None:
    run = start_run(store)
    with pytest.raises(ValueError, match="future"):
        store.transition(
            run.id, "user-1", StateChange(status="waiting_retry", retry_at=STARTED), now=STARTED
        )
    assert store.get_run(run.id, "user-1") == run


def test_operation_cannot_move_the_run_clock_backwards(store: RunStore) -> None:
    run = start_run(store)
    with pytest.raises(ValueError, match="precedes"):
        store.consume_budget(run.id, "user-1", BudgetCharge(model_requests=1), now=CREATED)
    assert store.get_run(run.id, "user-1") == run


@pytest.mark.parametrize("operation", ["get", "events", "transition", "budget"])
@pytest.mark.parametrize("user", ["other-user", "user-1"])
def test_user_ownership_and_unknown_run_have_the_same_error(
    store: RunStore, operation: str, user: str
) -> None:
    run = start_run(store)
    run_id = run.id if user == "other-user" else "unknown-run"
    with pytest.raises(RunNotFoundError, match="^run not found$"):
        if operation == "get":
            store.get_run(run_id, user)
        elif operation == "events":
            store.list_events(run_id, user)
        elif operation == "transition":
            store.transition(run_id, user, StateChange(status="completed"), now=STARTED)
        else:
            store.consume_budget(run_id, user, BudgetCharge(tool_calls=1), now=STARTED)
    assert store.get_run(run.id, "user-1") == run


def test_identifiers_are_parameterized_sql_data(store: RunStore) -> None:
    user = "user'; DROP TABLE runs; --"
    run = start_run(store, user_id=user)
    assert store.get_run(run.id, user).user_id == user
    assert len(store.list_events(run.id, user)) == 2


def test_all_budget_counters_are_saved_together(store: RunStore) -> None:
    run = start_run(store)
    charged = store.consume_budget(
        run.id,
        "user-1",
        BudgetCharge(model_requests=2, searches=1, fetches=1, tool_calls=3),
        now=STARTED,
    )
    assert charged.budget_usage == BudgetUsage(
        model_requests=2, searches=1, fetches=1, tool_calls=3
    )
    assert (
        store.list_events(run.id, "user-1")[-1].data["usage"] == charged.budget_usage.model_dump()
    )


@pytest.mark.parametrize("dimension", ["model_requests", "searches", "fetches", "tool_calls"])
def test_budget_exhaustion_does_not_increment_any_counter(store: RunStore, dimension: str) -> None:
    run = start_run(store)
    limit = getattr(run.budget_limits, dimension)
    full = store.consume_budget(run.id, "user-1", BudgetCharge(**{dimension: limit}), now=STARTED)
    with pytest.raises(BudgetExceededError) as caught:
        store.consume_budget(run.id, "user-1", BudgetCharge(**{dimension: 1}), now=STARTED)
    assert caught.value.dimension == dimension
    assert store.get_run(run.id, "user-1") == full
    assert len(store.list_events(run.id, "user-1")) == 3


def test_multi_counter_reservation_rolls_back_when_one_limit_is_exceeded(store: RunStore) -> None:
    run = start_run(store)
    with pytest.raises(BudgetExceededError):
        store.consume_budget(run.id, "user-1", BudgetCharge(searches=1, tool_calls=51), now=STARTED)
    assert store.get_run(run.id, "user-1").budget_usage == BudgetUsage()


@pytest.mark.parametrize("charge", [{}, {"tool_calls": -1}, {"tool_calls": 1.5}])
def test_invalid_budget_charge_is_rejected(charge: dict) -> None:
    with pytest.raises(ValidationError):
        BudgetCharge(**charge)


def test_queued_run_cannot_consume_budget(store: RunStore) -> None:
    run = store.create_run(request(), now=CREATED)
    with pytest.raises(InvalidRunStateError):
        store.consume_budget(run.id, "user-1", BudgetCharge(model_requests=1), now=CREATED)


@pytest.mark.parametrize("status", ["completed", "no_news"])
def test_deadline_blocks_new_calls_and_successful_completion(store: RunStore, status: str) -> None:
    run = start_run(store)
    with pytest.raises(BudgetExceededError) as caught:
        store.consume_budget(run.id, "user-1", BudgetCharge(model_requests=1), now=run.deadline_at)
    assert caught.value.dimension == "deadline"
    with pytest.raises(BudgetExceededError):
        store.transition(run.id, "user-1", StateChange(status=status), now=run.deadline_at)
    failed = store.transition(
        run.id,
        "user-1",
        StateChange(status="failed", stop_reason="deadline_exceeded"),
        now=run.deadline_at,
    )
    assert failed.status == "failed"


def test_expired_retry_cannot_restart_but_can_record_failure(store: RunStore) -> None:
    run = start_run(store)
    store.transition(
        run.id, "user-1", StateChange(status="waiting_retry", retry_at=run.deadline_at), now=STARTED
    )
    with pytest.raises(BudgetExceededError):
        store.transition(run.id, "user-1", StateChange(status="running"), now=run.deadline_at)
    failed = store.transition(
        run.id,
        "user-1",
        StateChange(status="failed", stop_reason="deadline_exceeded"),
        now=run.deadline_at,
    )
    assert failed.retry_at is None


def test_reopen_preserves_window_snapshot_usage_limits_and_deadline(
    store: RunStore, tmp_path: Path
) -> None:
    run = start_run(store)
    run = store.consume_budget(
        run.id, "user-1", BudgetCharge(model_requests=2, searches=1, tool_calls=2), now=STARTED
    )
    run = store.transition(
        run.id,
        "user-1",
        StateChange(status="waiting_retry", retry_at=STARTED + timedelta(seconds=10)),
        now=STARTED,
    )
    database = Database(store.database.path)
    database.initialize()
    changed = Settings(
        _env_file=None, data_dir=tmp_path, max_model_requests=99, run_timeout_seconds=900
    )
    reopened = RunStore(database, changed)
    assert reopened.get_run(run.id, "user-1") == run
    assert reopened.list_events(run.id, "user-1") == store.list_events(run.id, "user-1")
    resumed = reopened.transition(
        run.id, "user-1", StateChange(status="running"), now=STARTED + timedelta(seconds=10)
    )
    assert resumed.budget_limits.model_requests == 15
    assert resumed.deadline_at == run.deadline_at
    assert resumed.budget_usage == run.budget_usage
    assert resumed.window_start == run.window_start
    assert resumed.window_end == run.window_end
    assert reopened.create_run(request(), now=CREATED).budget_limits.model_requests == 99


@pytest.mark.parametrize("operation", ["create", "transition", "budget"])
def test_event_failure_rolls_back_business_state(
    store: RunStore, monkeypatch, operation: str
) -> None:
    run = start_run(store)
    before = store.list_events(run.id, "user-1")

    def fail_event(*args, **kwargs):
        raise RuntimeError("simulated interruption while saving event")

    monkeypatch.setattr(store, "_append_event", fail_event)
    with pytest.raises(RuntimeError, match="simulated"):
        if operation == "create":
            store.create_run(request(), now=CREATED)
        elif operation == "transition":
            store.transition(run.id, "user-1", StateChange(status="completed"), now=STARTED)
        else:
            store.consume_budget(run.id, "user-1", BudgetCharge(model_requests=1), now=STARTED)
    assert store.get_run(run.id, "user-1") == run
    assert store.list_events(run.id, "user-1") == before
    with store.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_concurrent_budget_consumption_does_not_lose_updates(store: RunStore) -> None:
    run = store.create_run(request())
    run = store.transition(run.id, "user-1", StateChange(status="running"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: store.consume_budget(run.id, "user-1", BudgetCharge(tool_calls=1)),
                range(8),
            )
        )
    assert sorted(result.budget_usage.tool_calls for result in results) == list(range(1, 9))
    assert store.get_run(run.id, "user-1").budget_usage.tool_calls == 8
    events = store.list_events(run.id, "user-1")
    assert len(events) == 10
    assert [event.occurred_at for event in events] == sorted(event.occurred_at for event in events)


def test_only_one_concurrent_start_succeeds(store: RunStore) -> None:
    run = store.create_run(request(), now=CREATED)

    def start():
        try:
            store.transition(run.id, "user-1", StateChange(status="running"), now=STARTED)
            return "started"
        except InvalidRunStateError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert sorted(results) == ["rejected", "started"]
    assert len(store.list_events(run.id, "user-1")) == 2


def test_event_pagination_and_privacy(store: RunStore) -> None:
    run = start_run(store)
    store.consume_budget(run.id, "user-1", BudgetCharge(model_requests=1), now=STARTED)
    first = store.list_events(run.id, "user-1", limit=2)
    second = store.list_events(run.id, "user-1", after_id=first[-1].id, limit=2)
    assert [event.kind for event in first + second] == [
        "run_created",
        "state_changed",
        "budget_consumed",
    ]
    serialized = " ".join(event.model_dump_json() for event in first + second)
    assert "reader@example.com" not in serialized
    assert "研究员" not in serialized


@pytest.mark.parametrize("pagination", [{"limit": 0}, {"limit": 501}, {"after_id": -1}])
def test_invalid_event_pagination_is_rejected(store: RunStore, pagination: dict) -> None:
    run = start_run(store)
    with pytest.raises(ValidationError):
        store.list_events(run.id, "user-1", **pagination)


def test_schema_rejects_inconsistent_run_state(store: RunStore) -> None:
    run = store.create_run(request(), now=CREATED)
    with pytest.raises(sqlite3.IntegrityError), store.database.transaction() as connection:
        connection.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run.id,))
    assert store.get_run(run.id, "user-1") == run
