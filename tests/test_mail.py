"""Persistent delivery against real loopback SMTP and controlled interruption points."""

import asyncio
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser

import pytest

import app.storage.database as database_module
from app.harness.execution import LeaseLostError
from app.product.models import ProductError
from app.providers.smtp import SendOutcome, SMTPTransport
from app.storage.dispatch import DispatchStore
from app.storage.models import timestamp
from app.worker.mail import Mailer
from tests.smtp_support import smtp_server, smtp_settings
from tests.test_auth import client_for, protection, register
from tests.test_product_api import client as client
from tests.test_product_api import put, seed_brief, subscribe


def reserve(client):
    brief, run = seed_brief(client, mode="scheduled")
    state = client.app.state
    owner = client.get("/me").json()["user"]["id"]
    with state.database.transaction() as connection:
        state.outbox_store.reserve(connection, run, owner, brief)
    return brief, run, owner


def configure(client, server):
    state = client.app.state
    config = smtp_settings(state.settings.data_dir, server.server_address[1])
    state.dispatch_store.settings = config
    state.smtp_transport = SMTPTransport(config)
    return Mailer(state.dispatch_store, state.smtp_transport)


def run(mailer):
    return asyncio.run(mailer.run_once())


def advance(client, seconds):
    clock = client.app.state.run_store.clock
    # The product fixture deliberately binds Clock.now; its mutable time controls retries/leases.
    clock.__self__.advance(seconds)


def status(client, brief):
    return client.get(f"/briefs/{brief}/mail").json()["mail"]


def test_real_delivery_is_frozen_unique_and_public_state_is_small(client):
    brief, run_id, owner = reserve(client)
    original = client.app.state.outbox_store.get(brief, owner)
    put(client, "/me", {"email": "new@example.com", "nickname": "新名字"})
    with smtp_server() as server:
        mailer = configure(client, server)
        assert run(mailer) == "sent" and run(mailer) is None
        assert len(server.messages) == 1
        message = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        assert message["To"] == original.recipient
        assert message.get_content().replace("\r\n", "\n").rstrip() == original.body_text
    public = status(client, brief)
    assert public["status"] == "sent" and public["attempts"] == 1
    assert not public["can_resend"] and not public["confirmation_required"]
    assert set(public) == {
        "brief_id",
        "status",
        "attempts",
        "updated_at",
        "retry_at",
        "error_code",
        "can_resend",
        "confirmation_required",
    }
    assert "recipient" not in str(public) and "token" not in str(public)
    events = client.get(f"/runs/{run_id}/events").json()["events"]
    assert "mail_sent" in [e["kind"] for e in events]
    assert original.recipient not in str(events)


def test_missing_configuration_preserves_pending_intent(client):
    brief, _, _ = reserve(client)
    assert run(client.app.state.mailer) is None
    assert status(client, brief)["status"] == "pending" and status(client, brief)["attempts"] == 0
    response = client.post(f"/briefs/{brief}/mail/resend", json={}, headers=protection(client))
    assert response.status_code == 503 and response.json()["error"]["code"] == "MAIL_UNAVAILABLE"


@pytest.mark.parametrize("when", ["before_claim", "before_data"])
def test_live_pause_prevents_transmission(client, when):
    brief, _, _ = reserve(client)
    callback = (lambda: subscribe(client, enabled=False)) if when == "before_data" else None
    with smtp_server(before_data=callback) as server:
        mailer = configure(client, server)
        if when == "before_claim":
            subscribe(client, enabled=False)
        result = run(mailer)
        assert result is None if when == "before_claim" else result == "failed"
        assert server.messages == []
    assert status(client, brief)["error_code"] == "SUBSCRIPTION_DISABLED"


def test_finite_retry_survives_reopen_and_waits_for_due_time(client):
    brief, _, owner = reserve(client)
    with smtp_server("temporary", "temporary", "temporary") as server:
        mailer = configure(client, server)
        assert run(mailer) == "pending" and run(mailer) is None
        assert status(client, brief)["retry_at"] is not None
        store = client.app.state.dispatch_store
        reopened = DispatchStore(store.outbox, store.briefs, store.settings)
        mailer = Mailer(reopened, mailer.transport)
        advance(client, 1)
        assert run(mailer) == "pending"
        advance(client, 1)
        assert run(mailer) == "failed" and run(mailer) is None
        assert server.connections == 3 and server.messages == []
    result = store.status(brief, owner)
    assert result["error_code"] == "MAIL_RETRY_EXHAUSTED" and result["attempts"] == 3
    with store.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mail_attempts").fetchone()[0] == 3


def test_lost_ack_unknown_requires_explicit_confirmation_and_reuses_message(client):
    brief, _, _ = reserve(client)
    path = f"/briefs/{brief}/mail/resend"
    with smtp_server("drop_after", "accept") as server:
        mailer = configure(client, server)
        assert run(mailer) == "unknown" and run(mailer) is None
        assert status(client, brief)["confirmation_required"]
        response = client.post(path, json={}, headers=protection(client))
        assert response.json()["error"]["code"] == "CONFIRM_DUPLICATE_DELIVERY"
        assert (
            client.post(
                path, json={"confirm_duplicate": True}, headers=protection(client)
            ).status_code
            == 202
        )
        assert client.post(path, json={}, headers=protection(client)).status_code == 409
        assert run(mailer) == "sent" and run(mailer) is None
        assert len(server.messages) == 2 and server.messages[0] == server.messages[1]
    assert status(client, brief)["attempts"] == 2
    assert len(client.get("/briefs").json()["items"]) == 1


def test_permanent_rejection_allows_manual_retry_but_pause_blocks_it(client):
    brief, _, owner = reserve(client)
    with smtp_server("reject", "accept") as server:
        mailer = configure(client, server)
        assert run(mailer) == "failed"
        subscribe(client, enabled=False)
        with pytest.raises(ProductError, match="SUBSCRIPTION_DISABLED"):
            mailer.store.resend(brief, owner)
        subscribe(client)
        assert mailer.store.resend(brief, owner)["status"] == "pending"
        assert run(mailer) == "sent"


def test_two_mailers_claim_only_once(client):
    brief, _, _ = reserve(client)
    with smtp_server() as server:
        mailer = configure(client, server)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: run(mailer), range(4)))
        assert results.count("sent") == 1 and len(server.messages) == 1
    assert status(client, brief)["attempts"] == 1


def test_expiry_conservatively_recovers_and_old_sender_cannot_finish(client):
    brief, _, owner = reserve(client)
    store = client.app.state.dispatch_store
    _, _, token = store.claim()
    assert store.admit(brief, token) and not store.admit(brief, "wrong")
    advance(client, 1000)
    assert not store.admit(brief, token) and store.recover_expired() == 1
    assert store.recover_expired() == 0 and store.claim() is None
    assert store.status(brief, owner)["status"] == "unknown"
    with pytest.raises(LeaseLostError):
        store.finish(brief, token, SendOutcome("sent"))
    store.resend(brief, owner, confirm_duplicate=True)
    _, _, new_token = store.claim()
    with pytest.raises(LeaseLostError):
        store.finish(brief, token, SendOutcome("sent"))
    advance(client, 1000)
    assert store.finish(brief, new_token, SendOutcome("sent")) == "unknown"


def test_mail_result_transaction_failure_is_recovered_as_unknown(client, monkeypatch):
    brief, _, _ = reserve(client)
    with smtp_server() as server:
        mailer = configure(client, server)
        original = mailer.store._event

        def fail(connection, row, kind, data):
            if kind == "mail_sent":
                raise sqlite3.OperationalError("fixture transaction failure")
            return original(connection, row, kind, data)

        monkeypatch.setattr(mailer.store, "_event", fail)
        with pytest.raises(sqlite3.OperationalError):
            run(mailer)
        assert status(client, brief)["status"] == "sending" and len(server.messages) == 1
        monkeypatch.setattr(mailer.store, "_event", original)
        advance(client, 1000)
        assert run(mailer) is None and status(client, brief)["status"] == "unknown"
        assert len(server.messages) == 1


def test_process_exit_after_smtp_acceptance_does_not_send_again(client):
    brief, _, _ = reserve(client)
    with smtp_server() as server:
        mailer = configure(client, server)
        process = subprocess.run(
            [
                __import__("sys").executable,
                "-m",
                "tests.mail_crash",
                str(client.app.state.settings.data_dir),
                str(server.server_address[1]),
                timestamp(client.app.state.run_store.now()),
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert process.returncode == 73, process.stderr.decode(errors="replace")
        assert len(server.messages) == 1 and status(client, brief)["status"] == "sending"
        advance(client, 1000)
        assert run(mailer) is None and status(client, brief)["status"] == "unknown"
        assert server.connections == 1


def test_mail_api_ownership_csrf_input_and_preview(client):
    brief, _, _ = reserve(client)
    preview, _ = seed_brief(client)
    assert status(client, preview) is None
    with smtp_server() as server:
        configure(client, server)
        path = f"/briefs/{brief}/mail/resend"
        assert client.post(path, json={}).status_code == 403
        for body in ({"confirm_duplicate": "true"}, {"recipient": "foreign@example.com"}):
            assert client.post(path, json=body, headers=protection(client)).status_code == 422
        assert client.get(f"/briefs/{brief}/mail?user_id=other").status_code == 422
        with client_for(client.app.state.settings) as other:
            assert other.get(f"/briefs/{brief}/mail").status_code == 401
            register(other, email="foreign@example.com")
            assert other.get(f"/briefs/{brief}/mail").status_code == 404
            other.app.state.smtp_transport = client.app.state.smtp_transport
            assert other.post(path, json={}, headers=protection(other)).status_code == 404


def test_v16_migration_is_atomic_and_preserves_pending_mail(client, monkeypatch):
    brief, _, owner = reserve(client)
    state = client.app.state
    with state.database.transaction() as connection:
        connection.execute("DROP TABLE mail_attempts")
        connection.execute("DROP TABLE mail_dispatch")
        connection.execute("PRAGMA user_version=15")
    schema = database_module.SCHEMA_V16
    monkeypatch.setattr(database_module, "SCHEMA_V16", (*schema, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        state.database.initialize()
    with state.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='mail_dispatch'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V16", schema)
    state.database.initialize()
    assert state.outbox_store.get(brief, owner).status == "pending"
