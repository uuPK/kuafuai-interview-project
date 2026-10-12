"""Filter configured credentials and common credential fields before persistence."""

import re

from app.config import Settings

SENSITIVE_KEYS = {"apikey", "password", "secret", "token", "authorization", "cookie", "setcookie"}


class Redactor:
    def __init__(self, settings: Settings):
        self.secrets = tuple(
            value.get_secret_value()
            for name in (
                "deepseek_api_key",
                "bocha_api_key",
                "smtp_username",
                "smtp_password",
                "session_secret",
            )
            if (value := getattr(settings, name)) is not None
        )

    def contains_credential(self, text: str) -> bool:
        return any(secret in text for secret in self.secrets) or bool(
            re.search(r"(?i)\bsk-[a-z0-9_-]{8,}", text)
        )

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)\bsk-[a-z0-9_-]{8,}", "[REDACTED]", value)
            return re.sub(
                r"(?im)\b(authorization|set-cookie|cookie)\s*[:=][^\r\n]+",
                r"\1: [REDACTED]",
                value,
            )
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {
                self.clean(key): "[REDACTED]"
                if re.sub(r"[^a-z]", "", key.lower()) in SENSITIVE_KEYS
                else self.clean(item)
                for key, item in value.items()
            }
        return value
