from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.storage.schedules import ScheduleStore, next_slot
from tests.test_product_api import client as _client
from tests.test_product_api import subscribe

client = _client


def clock_at(client, value):
    client.app.state.run_store.clock = lambda: datetime.fromisoformat(value)
    return client.app.state


def test_default_nine_and_fixed_window_with_repeated_ticks(client):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    saved = subscribe(client).json()["subscription"]
    assert saved["schedule"]["next_at"] == "2026-10-15T01:00:00.000000+00:00"
    assert state.schedule_store.tick() == 0
    clock_at(client, "2026-10-15T01:00:00+00:00")
    assert state.schedule_store.tick() == 1
    assert state.schedule_store.tick() == 0
    run = state.catalog_store.runs_page(client.get("/me").json()["user"]["id"])["items"][0]
    assert run["mode"] == "scheduled" and run["window_end"] == "2026-10-15T01:00:00Z"
    assert run["window_start"] == "2026-10-14T01:00:00Z"
    assert run["status"] == "queued"


@pytest.mark.parametrize("elapsed,created", [(0, 1), (7199, 1), (7200, 1), (7201, 0), (100000, 0)])
def test_two_hour_catchup_boundary_and_missed_notice(client, elapsed, created):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    at = datetime(2026, 10, 15, 1, tzinfo=UTC) + timedelta(seconds=elapsed)
    state.run_store.clock = lambda: at
    assert state.schedule_store.tick() == created
    assert state.schedule_store.tick() == 0
    summary = client.get("/subscription").json()["subscription"]["schedule"]
    if not created:
        assert summary["last_notice"]["reason"] == "outside_catchup_window"
        assert summary["last_notice"]["missed_count"] >= 1


def test_changed_time_same_day_never_creates_second_run(client):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    clock_at(client, "2026-10-15T01:00:00+00:00")
    assert state.schedule_store.tick() == 1
    subscribe(client, push_time="10:00")
    clock_at(client, "2026-10-15T02:00:00+00:00")
    assert state.schedule_store.tick() == 0
    assert (
        client.get("/subscription")
        .json()["subscription"]["schedule"]["next_at"]
        .startswith("2026-10-16T02:00")
    )


def test_pause_resume_and_new_subscription_after_slot_start_tomorrow(client):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client, enabled=False)
    clock_at(client, "2026-10-15T01:10:00+00:00")
    assert state.schedule_store.tick() == 0
    resumed = subscribe(client).json()["subscription"]
    assert resumed["schedule"]["next_at"].startswith("2026-10-16T01:00")
    assert state.schedule_store.tick() == 0
    clock_at(client, "2026-10-16T01:00:00+00:00")
    assert state.schedule_store.tick() == 1


def test_preference_only_change_preserves_due_and_snapshot(client):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    clock_at(client, "2026-10-15T01:30:00+00:00")
    subscribe(client, topics=["AI芯片"], max_items=2)
    assert state.schedule_store.tick() == 1
    with state.database.connection() as connection:
        row = connection.execute("SELECT id,user_id FROM runs").fetchone()
    run = state.run_store.get_run(row["id"], row["user_id"])
    assert run.subscription.topics == ("AI芯片",) and run.subscription.max_items == 2
    assert run.window_end.hour == 1


def test_cold_store_and_parallel_schedulers_claim_once(client):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    clock_at(client, "2026-10-15T01:59:00+00:00")
    stores = [ScheduleStore(state.run_store, state.subscription_store) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(lambda store: store.tick(), stores))
    assert sum(result) == 1
    with state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_long_downtime_records_range_without_creating_old_runs(client):
    state = clock_at(client, "2026-10-01T00:00:00+00:00")
    subscribe(client)
    clock_at(client, "2026-10-15T01:30:00+00:00")
    assert state.schedule_store.tick() == 1
    with state.database.connection() as connection:
        notice = connection.execute("SELECT * FROM schedule_notices").fetchone()
    assert notice["missed_count"] == 14
    assert notice["first_at"].startswith("2026-10-01T01:00")
    assert notice["last_at"].startswith("2026-10-14T01:00")


def test_bad_subscription_is_visible_and_can_be_repaired(client):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    with state.database.transaction() as connection:
        connection.execute("UPDATE subscriptions SET preferences_json='broken'")
    clock_at(client, "2026-10-15T01:00:00+00:00")
    assert state.schedule_store.tick() == 0
    assert subscribe(client).status_code == 200
    assert state.schedule_store.tick() == 1


def test_create_failure_rolls_back_cursor_and_daily_key(client, monkeypatch):
    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    clock_at(client, "2026-10-15T01:00:00+00:00")
    original = state.run_store._append_event

    def crash(*args):
        raise RuntimeError("fixture crash")

    monkeypatch.setattr(state.run_store, "_append_event", crash)
    with pytest.raises(RuntimeError):
        state.schedule_store.tick()
    monkeypatch.setattr(state.run_store, "_append_event", original)
    assert state.schedule_store.tick() == 1


def test_timezone_midnight_and_new_time_in_past(client):
    at = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    assert next_slot(at, "00:00") == at
    assert next_slot(at + timedelta(seconds=1), "00:00") == at + timedelta(days=1)


def test_migrated_subscription_bootstrap_and_schema_rollback(client, monkeypatch):
    import app.storage.database as database_module

    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    with state.database.connection() as connection:
        for table in ("schedule_notices", "subscription_schedules", "scheduled_runs"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=14")
    schema = database_module.SCHEMA_V15
    monkeypatch.setattr(database_module, "SCHEMA_V15", (*schema, "INVALID SQL"))
    with pytest.raises(Exception, match="syntax"):
        state.database.initialize()
    with state.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
    monkeypatch.setattr(database_module, "SCHEMA_V15", schema)
    state.database.initialize()
    state.database.initialize()
    clock_at(client, "2026-10-15T01:10:00+00:00")
    assert state.schedule_store.tick() == 1


def test_worker_maintenance_schedules_before_claiming(client):
    import asyncio

    from app.agent.runner import AgentRunner
    from app.worker.__main__ import UnconfiguredModel
    from app.worker.service import Worker

    state = clock_at(client, "2026-10-15T00:00:00+00:00")
    subscribe(client)
    clock_at(client, "2026-10-15T01:00:00+00:00")

    async def execute():
        runner = AgentRunner(
            UnconfiguredModel(), state.tool_executor, state.conversation_store, state.settings
        )

        async def maintenance():
            state.schedule_store.tick()

        worker = Worker(runner, maintenance=maintenance)
        try:
            run = await worker.run_once()
            assert run.mode == "scheduled" and run.status == "failed"
            assert run.stop_reason == "model_dependency_configuration"
            assert await worker.run_once() is None
        finally:
            worker.leases.stop_worker(worker.worker_id)

    asyncio.run(execute())
