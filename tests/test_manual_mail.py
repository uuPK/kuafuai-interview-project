"""Explicit delivery of saved editions; SMTP is loopback only."""

from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser

import pytest

from tests.smtp_support import smtp_server
from tests.test_auth import protection, register
from tests.test_mail import configure, reserve, run, status
from tests.test_product_api import client as client
from tests.test_product_api import put, seed_brief, subscribe


def send(client, brief, **kwargs):
    return client.post(f"/briefs/{brief}/mail", json={}, headers=protection(client), **kwargs)


def test_preview_manual_delivery_is_frozen_idempotent_and_sent_once(client):
    brief, _ = seed_brief(client)
    assert status(client, brief) is None
    put(client, "/me", {"email": "current@example.com", "nickname": "读者"})
    with smtp_server() as server:
        mailer = configure(client, server)
        mailer.transport.settings = mailer.transport.settings.model_copy(
            update={"smtp_test_recipient": "current@example.com"}
        )
        headers = protection(client)
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(
                pool.map(
                    lambda _: client.post(f"/briefs/{brief}/mail", json={}, headers=headers),
                    range(3),
                )
            )
        assert all(response.status_code == 202 for response in responses)
        assert all(response.json()["recipient"] == "current@example.com" for response in responses)
        assert status(client, brief)["status"] == "pending"
        put(client, "/me", {"email": "later@example.com", "nickname": "读者"})
        assert send(client, brief).json()["recipient"] == "current@example.com"
        assert run(mailer) == "sent"
        assert run(mailer) is None
        assert send(client, brief).json()["mail"]["status"] == "sent"
        assert run(mailer) is None
        assert len(server.messages) == 1
        message = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        assert message["To"] == "current@example.com"
        assert "[fixture] 暂无新闻" in message.get_content()
    result = client.get(f"/briefs/{brief}/mail").json()
    assert result["recipient"] == "current@example.com"
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM mail_attempts").fetchone()[0] == 1


@pytest.mark.parametrize("case", ["csrf", "extra", "unavailable", "paused", "foreign", "missing"])
def test_manual_mail_permissions_and_configuration_never_enqueue(client, case):
    brief, _ = seed_brief(client)
    path = f"/briefs/{brief}/mail"
    with smtp_server() as server:
        configure(client, server)
        if case == "csrf":
            response = client.post(path, json={})
            expected = 403
        elif case == "extra":
            response = client.post(
                path, json={"recipient": "arbitrary@example.com"}, headers=protection(client)
            )
            expected = 422
        else:
            if case == "unavailable":
                client.app.state.smtp_transport = type("Unavailable", (), {"ready": False})()
            elif case == "paused":
                subscribe(client, enabled=False)
            elif case == "foreign":
                assert register(client, email="other@example.com").status_code == 201
            elif case == "missing":
                brief = "brief-" + "0" * 32
            response = send(client, brief)
            expected = {"unavailable": 503, "paused": 409, "foreign": 404, "missing": 404}[case]
        assert response.status_code == expected
        assert not server.messages
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 0


def test_manual_action_reuses_scheduled_message_and_cannot_bypass_unknown_confirmation(client):
    brief, _, owner = reserve(client)
    original = client.app.state.outbox_store.get(brief, owner)
    put(client, "/me", {"email": "later@example.com", "nickname": "读者"})
    with smtp_server() as server:
        configure(client, server)
        assert send(client, brief).json()["recipient"] == original.recipient
        with client.app.state.database.transaction() as connection:
            connection.execute("UPDATE mail_outbox SET status='unknown' WHERE brief_id=?", (brief,))
        assert send(client, brief).json()["mail"]["status"] == "unknown"
        assert client.app.state.outbox_store.get(brief, owner).payload_hash == original.payload_hash
        denied = client.post(f"/briefs/{brief}/mail/resend", json={}, headers=protection(client))
        assert denied.json()["error"]["code"] == "CONFIRM_DUPLICATE_DELIVERY"
        assert not server.messages


def test_manual_mail_reports_recipient_allowlist_before_enqueue(client):
    brief, _ = seed_brief(client)
    put(client, "/me", {"email": "outside@example.com", "nickname": "读者"})
    with smtp_server() as server:
        configure(client, server)
        delivery = client.get(f"/briefs/{brief}/mail").json()
        assert delivery["delivery_available"] is False
        assert delivery["unavailable_reason"] == "MAIL_TEST_RECIPIENT_MISMATCH"
        assert delivery["recipient"] == "outside@example.com"
        assert "reader@example.com" not in str(delivery)
        response = send(client, brief)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MAIL_TEST_RECIPIENT_MISMATCH"
        assert run(configure(client, server)) is None
        assert status(client, brief) is None
        assert not server.messages
