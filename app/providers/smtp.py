"""Submission client: explicit rejections can retry; uncertain DATA never auto-retries."""

import ipaddress
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime


def mailbox(value):
    return (
        isinstance(value, str)
        and len(value) <= 254
        and re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", value) is not None
        and ".." not in value
        and not value.startswith(".")
        and not value.split("@")[0].endswith(".")
        and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
            for part in value.split("@")[-1].split(".")
        )
    )


@dataclass(frozen=True)
class SendOutcome:
    status: str
    code: str | None = None


def rejected(code):
    return SendOutcome("retry" if 400 <= code < 500 else "failed", "SMTP_REJECTED")


class SMTPTransport:
    def __init__(self, settings, *, factory=None):
        self.settings = settings
        self.factory = factory

    @property
    def ready(self):
        return self.settings.smtp_enabled and not self.settings.missing_service_fields("smtp")

    def send(self, record, message_id, *, admit=lambda: True):
        s = self.settings
        if not self.ready:
            return SendOutcome("failed", "SMTP_CONFIGURATION")
        if not mailbox(s.smtp_from) or not mailbox(record.recipient):
            return SendOutcome("failed", "MAIL_ADDRESS_INVALID")
        if (
            s.smtp_test_recipient
            and record.recipient.casefold() != s.smtp_test_recipient.casefold()
        ):
            return SendOutcome("failed", "MAIL_TEST_RECIPIENT_MISMATCH")
        host = s.smtp_host
        if len(host) > 253 or any(c.isspace() or c in "/@?#\\\x00" for c in host):
            return SendOutcome("failed", "SMTP_CONFIGURATION")
        if s.smtp_tls_mode == "none":
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host.casefold() == "localhost"
            if not local or s.smtp_username or s.smtp_password:
                return SendOutcome("failed", "SMTP_TLS_REQUIRED")
        message = EmailMessage(policy=SMTP)
        try:
            message["From"] = s.smtp_from
            message["To"] = record.recipient
            message["Subject"] = record.subject
            message["Message-ID"] = message_id
            message["Date"] = format_datetime(record.created_at)
            message.set_content(record.body_text, charset="utf-8", cte="quoted-printable")
            encoded = message.as_bytes()
            if len(encoded) > 2_000_000:
                return SendOutcome("failed", "MAIL_TOO_LARGE")
        except (ValueError, UnicodeError):
            return SendOutcome("failed", "MAIL_HEADER_INVALID")
        client = None
        data_started = False
        deadline = time.monotonic() + s.request_timeout_seconds

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError
            if client is not None and client.sock is not None:
                client.sock.settimeout(seconds)
            return seconds

        try:
            factory = self.factory or (
                smtplib.SMTP_SSL if s.smtp_tls_mode == "ssl" else smtplib.SMTP
            )
            kwargs = {"timeout": remaining(), "local_hostname": "daily-ai.local"}
            if s.smtp_tls_mode == "ssl":
                kwargs["context"] = ssl.create_default_context()
            client = factory(host, s.smtp_port, **kwargs)
            remaining()
            client.ehlo_or_helo_if_needed()
            if s.smtp_tls_mode == "starttls":
                remaining()
                client.starttls(context=ssl.create_default_context())
                remaining()
                client.ehlo()
            if s.smtp_username:
                remaining()
                client.login(s.smtp_username.get_secret_value(), s.smtp_password.get_secret_value())
            remaining()
            # Re-read live subscription and capability immediately before the envelope.
            if not admit():
                return SendOutcome("failed", "SUBSCRIPTION_DISABLED")
            code, _ = client.mail(s.smtp_from)
            if not 200 <= code < 300:
                return rejected(code)
            remaining()
            code, _ = client.rcpt(record.recipient)
            if not 200 <= code < 300:
                return rejected(code)
            remaining()
            if not admit():
                return SendOutcome("failed", "SUBSCRIPTION_DISABLED")
            data_started = True
            code, _ = client.data(encoded)
            if code == 250:
                return SendOutcome("sent")
            if 400 <= code < 600:
                return rejected(code)
            return SendOutcome("unknown", "SMTP_RESULT_UNKNOWN")
        except smtplib.SMTPResponseException as error:
            return rejected(error.smtp_code)
        except (ssl.SSLError, smtplib.SMTPNotSupportedError, ValueError, UnicodeError):
            return (
                SendOutcome("unknown", "SMTP_RESULT_UNKNOWN")
                if data_started
                else SendOutcome("failed", "SMTP_SECURITY_OR_CONFIGURATION")
            )
        except (OSError, smtplib.SMTPException):
            return (
                SendOutcome("unknown", "SMTP_RESULT_UNKNOWN")
                if data_started
                else SendOutcome("retry", "SMTP_TEMPORARY")
            )
        except Exception:
            return (
                SendOutcome("unknown", "SMTP_RESULT_UNKNOWN")
                if data_started
                else SendOutcome("failed", "SMTP_CLIENT_ERROR")
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass
