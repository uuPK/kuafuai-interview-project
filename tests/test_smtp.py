import smtplib
import ssl
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.providers.smtp import SMTPTransport, mailbox
from tests.smtp_support import smtp_server, smtp_settings


def record(**changes):
    return SimpleNamespace(
        **{
            "recipient": "reader@example.com",
            "subject": "每日 AI 新闻｜测试",
            "body_text": "中文正文\n.行首句点\n来源：https://example.com/news",
            "created_at": datetime(2026, 10, 15, tzinfo=UTC),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "mode,status",
    [
        ("accept", "sent"),
        ("temporary", "retry"),
        ("reject", "failed"),
        ("drop_before", "retry"),
        ("drop_after", "unknown"),
    ],
)
def test_real_loopback_protocol(tmp_path, mode, status):
    with smtp_server(mode) as server:
        result = SMTPTransport(smtp_settings(tmp_path, server.server_address[1])).send(
            record(), "<fixture@daily-ai.invalid>"
        )
        assert result.status == status
        if mode in {"accept", "drop_after"}:
            parsed = BytesParser(policy=policy.default).parsebytes(server.messages[0])
            assert parsed["To"] == "reader@example.com"
            assert str(parsed["Subject"]) == record().subject
            assert parsed["Message-ID"] == "<fixture@daily-ai.invalid>"
            assert parsed.get_content().replace("\r\n", "\n").strip() == record().body_text
        else:
            assert server.messages == []


class FakeClient:
    def __init__(self, *, fail=None, code=250):
        self.calls = []
        self.sock = SimpleNamespace(settimeout=lambda t: self.calls.append("timeout"))
        self.fail, self.code = fail, code

    def ehlo_or_helo_if_needed(self):
        self.calls.append("hello")

    def starttls(self, *, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        self.calls.append("tls")
        if self.fail == "tls":
            raise ssl.SSLError("fixture certificate")

    def ehlo(self):
        self.calls.append("ehlo")

    def login(self, user, password):
        self.calls.append("login")
        assert (user, password) == ("fixture-user", "fixture-password")

    def mail(self, sender):
        self.calls.append("mail")
        if self.fail == "mail":
            raise TimeoutError("fixture")
        return 250, b"OK"

    def rcpt(self, recipient):
        self.calls.append("rcpt")
        return self.code, b"fixture recipient"

    def data(self, data):
        self.calls.append("data")
        if isinstance(self.fail, Exception):
            raise self.fail
        return self.code, b"fixture result"

    def close(self):
        self.calls.append("close")


def transport(tmp_path, fake, **changes):
    options = smtp_settings(tmp_path, 25).model_copy(update=changes)

    def factory(host, port, **kwargs):
        assert kwargs["timeout"] > 0
        if options.smtp_tls_mode == "ssl":
            assert kwargs["context"].check_hostname
        return fake

    return SMTPTransport(options, factory=factory)


@pytest.mark.parametrize("tls", ["starttls", "ssl"])
def test_tls_and_auth_order(tmp_path, tls):
    fake = FakeClient()
    sender = transport(
        tmp_path,
        fake,
        smtp_tls_mode=tls,
        smtp_username=SecretStr("fixture-user"),
        smtp_password=SecretStr("fixture-password"),
    )
    assert sender.send(record(), "<fixture@x>").status == "sent"
    calls = [v for v in fake.calls if v != "timeout"]
    expected = ["hello", "tls", "ehlo"] if tls == "starttls" else ["hello"]
    assert calls == [*expected, "login", "mail", "rcpt", "data", "close"]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"smtp_enabled": False}, "SMTP_CONFIGURATION"),
        ({"smtp_host": None}, "SMTP_CONFIGURATION"),
        ({"smtp_from": "sender@example.com\r\nBcc:x@y"}, "MAIL_ADDRESS_INVALID"),
        ({"smtp_test_recipient": "another@example.com"}, "MAIL_TEST_RECIPIENT_MISMATCH"),
        ({"smtp_host": "external.example.com"}, "SMTP_TLS_REQUIRED"),
        ({"smtp_host": "bad/host"}, "SMTP_CONFIGURATION"),
        ({"smtp_username": SecretStr("a"), "smtp_password": SecretStr("b")}, "SMTP_TLS_REQUIRED"),
    ],
)
def test_configuration_boundaries_do_not_connect(tmp_path, changes, code):
    fake = FakeClient()
    assert transport(tmp_path, fake, **changes).send(record(), "<x@y>").code == code
    assert fake.calls == []


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"recipient": "reader@example.com,other@example.com"}, "MAIL_ADDRESS_INVALID"),
        ({"subject": "test\r\nBcc:x@y"}, "MAIL_HEADER_INVALID"),
        ({"body_text": "中" * 800000}, "MAIL_TOO_LARGE"),
    ],
)
def test_payload_rejected_before_connect(tmp_path, changes, code):
    fake = FakeClient()
    assert transport(tmp_path, fake).send(record(**changes), "<x@y>").code == code
    assert fake.calls == []


@pytest.mark.parametrize(
    "failure,status",
    [
        (TimeoutError(), "unknown"),
        (smtplib.SMTPServerDisconnected(), "unknown"),
        (RuntimeError(), "unknown"),
        (smtplib.SMTPDataError(451, b"private text"), "retry"),
        (smtplib.SMTPDataError(550, b"private text"), "failed"),
    ],
)
def test_data_failures_are_classified_without_remote_text(tmp_path, failure, status):
    result = transport(tmp_path, FakeClient(fail=failure)).send(record(), "<x@y>")
    assert result.status == status and "private" not in repr(result)


def test_admission_rechecked_before_data_and_security_failure(tmp_path):
    fake = FakeClient()
    decisions = iter([True, False])
    result = transport(tmp_path, fake).send(record(), "<x@y>", admit=lambda: next(decisions))
    assert result.code == "SUBSCRIPTION_DISABLED" and "data" not in fake.calls
    assert (
        transport(tmp_path, FakeClient(fail="tls"), smtp_tls_mode="starttls")
        .send(record(), "<x@y>")
        .status
        == "failed"
    )


@pytest.mark.parametrize(
    "address",
    [
        "a..b@example.com",
        ".a@example.com",
        "a.@example.com",
        "a@-bad.com",
        "a@",
        "中@example.com",
        "a@b..c",
    ],
)
def test_mailbox_injection_and_syntax(address):
    assert not mailbox(address)
