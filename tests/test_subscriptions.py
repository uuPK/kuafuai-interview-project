import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import SecretStr

import app.storage.database as database_module
from app.auth.models import AuthError
from app.product.models import ProductError, ProfileInput, SubscriptionInput
from app.product.paging import Cursors
from app.storage.database import Database
from app.storage.models import StateChange
from app.storage.runs import RunStore
from app.storage.subscriptions import SubscriptionStore
from app.tools.models import ToolFailure
from tests.test_product_api import client as _client
from tests.test_product_api import preview, put, seed_brief, subscribe

client = _client


def test_parallel_previews_share_one_queued_run_and_subscription(client):
    saved = subscribe(client).json()["subscription"]
    owner = client.get("/me").json()["user"]["id"]
    store = client.app.state.subscription_store
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: store.preview(owner), range(6)))
    assert len({result[0] for result in results}) == 1
    assert sum(not result[1] for result in results) == 1
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
    assert store.get(owner)["id"] == saved["id"]


def test_preview_insert_and_event_failure_roll_back_together(client, monkeypatch):
    subscribe(client)
    runs = client.app.state.run_store

    def crash(*args):
        raise RuntimeError("fixture interrupt before COMMIT")

    monkeypatch.setattr(runs, "_append_event", crash)
    owner = client.get("/me").json()["user"]["id"]
    with pytest.raises(RuntimeError):
        client.app.state.subscription_store.preview(owner)
    with runs.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


def test_concurrent_profile_and_subscription_changes_keep_coherent_snapshot(client):
    subscribe(client)
    state = client.app.state
    owner = client.get("/me").json()["user"]["id"]
    profiles = [
        ProfileInput(email=f"profile{i}@example.com", nickname=f"reader-{i}") for i in range(2)
    ]
    forms = [SubscriptionInput(topics=[f"topic-{i}"], max_items=i + 1) for i in range(2)]
    # Each write is independent, but each read must take whole records from one DB snapshot.
    state.subscription_store.profile(owner, profiles[0])
    state.subscription_store.save(owner, forms[0])

    def modify():
        for i in range(8):
            state.subscription_store.profile(owner, profiles[i % 2])
            state.subscription_store.save(owner, forms[i % 2])

    def create():
        snapshots = []
        for _ in range(8):
            identifier, _ = state.subscription_store.preview(owner)
            run = state.run_store.get_run(identifier, owner)
            snapshots.append(run.subscription)
            state.run_store.transition(identifier, owner, StateChange(status="running"))
            state.run_store.transition(
                identifier, owner, StateChange(status="failed", stop_reason="fixture_finished")
            )
        return snapshots

    with ThreadPoolExecutor(max_workers=2) as pool:
        modifying = pool.submit(modify)
        creating = pool.submit(create)
        modifying.result()
        snapshots = creating.result()
    assert all(
        (s.email, s.nickname) in {(p.email, p.nickname) for p in profiles} for s in snapshots
    )
    assert all(
        (s.topics, s.max_items) in {(tuple(f.topics), f.max_items) for f in forms}
        for s in snapshots
    )


def test_live_pause_and_owner_subscription_check_control_actual_outbox(client):
    brief_id, run_id = seed_brief(client, mode="scheduled")
    state = client.app.state
    owner = client.get("/me").json()["user"]["id"]
    assert subscribe(client, enabled=False).status_code == 200
    run = state.run_store.get_run(run_id, owner)
    assert run.subscription.enabled is True
    with state.database.transaction() as connection:
        assert state.outbox_store.reserve(connection, run_id, owner, brief_id) is None
        assert (
            state.subscription_store.delivery_enabled(
                connection, run.model_copy(update={"user_id": "foreign"})
            )
            is False
        )
    with state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 0
    assert any(e.kind == "mail_suppressed" for e in state.run_store.list_events(run_id, owner))
    subscribe(client, enabled=True)
    put(client, "/me", {"email": "new@example.com", "nickname": "new"})
    with state.database.transaction() as connection:
        record = state.outbox_store.reserve(connection, run_id, owner, brief_id)
    assert record.recipient == "reader@example.com" and record.status == "pending"


def test_live_subscription_corruption_cannot_suppress_or_authorize_silently(client):
    brief_id, run_id = seed_brief(client, mode="scheduled")
    state = client.app.state
    owner = client.get("/me").json()["user"]["id"]
    with state.database.transaction() as connection:
        connection.execute("UPDATE subscriptions SET preferences_json='{}'")
    with pytest.raises(ToolFailure) as error, state.database.transaction() as connection:
        state.outbox_store.reserve(connection, run_id, owner, brief_id)
    assert error.value.result.error.code == "SUBSCRIPTION_STATE_PROBLEM"
    with state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 0


def test_missing_account_foreign_subscription_and_new_runtime_use_server_owner(client):
    subscribe(client)
    store = client.app.state.subscription_store
    with pytest.raises(AuthError):
        store.save("missing", SubscriptionInput(topics=["AI"]))
    with pytest.raises(AuthError):
        store.profile("missing", ProfileInput(email="x@example.com", nickname="reader"))
    # A separate store/connection reads the same committed subscription and pending task.
    reopened = SubscriptionStore(
        RunStore(Database(store.database.path), client.app.state.settings),
        client.app.state.settings,
    )
    owner = client.get("/me").json()["user"]["id"]
    assert reopened.get(owner) == store.get(owner)
    assert reopened.preview(owner)[0] == preview(client).json()["run_id"]


def test_v14_upgrade_failure_preserves_account_session_and_run(client, monkeypatch):
    subscribe(client)
    run_id = preview(client).json()["run_id"]
    state = client.app.state
    account = client.get("/me").json()["user"]
    before = state.run_store.get_run(run_id, account["id"])
    with state.database.connection() as connection:
        connection.execute("DROP TABLE subscriptions")
        connection.execute("PRAGMA user_version=13")
    statements = database_module.SCHEMA_V14
    monkeypatch.setattr(database_module, "SCHEMA_V14", (*statements, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        state.database.initialize()
    assert client.get("/me").json()["user"] == account
    with state.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='subscriptions'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V14", statements)
    state.database.initialize()
    state.database.initialize()
    assert state.run_store.get_run(run_id, account["id"]) == before
    assert client.get("/subscription").json()["subscription"] is None


def test_cursor_validation_and_store_page_bounds(client):
    state = client.app.state
    cursors = Cursors(state.settings)
    owner = client.get("/me").json()["user"]["id"]
    at = "2026-10-15T00:00:00.000000+00:00"
    assert cursors.decode(owner, "runs", cursors.encode(owner, "runs", at, str(uuid4())))
    for created, identifier in (("naive", "id"), ("2026-10-15T00:00:00", "id"), (at, ""), (at, 3)):
        with pytest.raises(ProductError):
            cursors.decode(owner, "runs", cursors.encode(owner, "runs", created, identifier))
    for cursor in ("x" * 513, "bad", "!.signature", "a.b.c", 1):
        with pytest.raises(ProductError):
            cursors.decode(owner, "runs", cursor)
    for limit in (0, 51, True, "2"):
        with pytest.raises(ProductError):
            state.catalog_store.runs_page(owner, limit=limit)


def test_credential_check_uses_raw_values_before_json_escaping(client):
    before = subscribe(client).json()["subscription"]
    state = client.app.state
    owner = client.get("/me").json()["user"]["id"]
    secret = 'fixture "quoted" \\credential'
    settings = state.settings.model_copy(update={"smtp_password": SecretStr(secret)})
    store = SubscriptionStore(state.run_store, settings)
    with pytest.raises(ProductError) as error:
        store.save(owner, SubscriptionInput(topics=["AI " + secret]))
    assert error.value.code == "SENSITIVE_INPUT"
    with pytest.raises(ProductError):
        store.profile(owner, ProfileInput(email="x@example.com", nickname=secret))
    assert store.get(owner) == before
