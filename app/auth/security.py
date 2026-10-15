import hashlib
import hmac
import re
import secrets
from functools import cached_property

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

from app.auth.models import AuthError

TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def mac(settings, purpose, value):
    if settings.session_secret is None:
        raise AuthError("AUTH_UNAVAILABLE", 503)
    return hmac.new(
        settings.session_secret.get_secret_value().encode(),
        (purpose + ":" + value).encode(),
        hashlib.sha256,
    ).hexdigest()


def csrf(settings, token, *, anonymous=False):
    return mac(settings, "anonymous-csrf" if anonymous else "session-csrf", token)


def anonymous(settings, now):
    token = str(int(now.timestamp())) + "." + secrets.token_urlsafe(32)
    return token, csrf(settings, token, anonymous=True)


def valid_anonymous(token, now):
    if not isinstance(token, str) or len(token) > 64:
        return False
    parts = token.split(".")
    return (
        len(parts) == 2
        and parts[0].isdigit()
        and TOKEN.fullmatch(parts[1]) is not None
        and 0 <= now.timestamp() - int(parts[0]) < 600
    )


def valid_csrf(settings, token, supplied, *, anonymous=False):
    return (
        isinstance(supplied, str)
        and re.fullmatch(r"[a-f0-9]{64}", supplied) is not None
        and hmac.compare_digest(csrf(settings, token, anonymous=anonymous), supplied)
    )


class Passwords:
    def __init__(self):
        self.hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)

    @cached_property
    def dummy(self):
        return self.hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password):
        return self.hasher.hash(password)

    def verify(self, password_hash, password):
        try:
            self.hasher.verify(password_hash or self.dummy, password)
            if password_hash is None:
                return False, None
            return True, self.hash(password) if self.hasher.check_needs_rehash(
                password_hash
            ) else None
        except (InvalidHashError, VerificationError):
            return False, None
