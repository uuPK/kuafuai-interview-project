"""Short SQLite transactions; no raw password/session token is persisted."""

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.auth.models import AuthError, User
from app.auth.security import TOKEN, mac
from app.storage.models import as_utc, timestamp


def user(row):
    return User(**{name: row[name] for name in User.model_fields})


class AuthStore:
    def __init__(self, database, settings, *, clock=None):
        self.database, self.settings = database, settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def now(self):
        return as_utc(self.clock())

    def key(self, token):
        return mac(self.settings, "session-store", token)

    def authenticate(self, token):
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            return None
        key = self.key(token)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT u.* FROM auth_users u JOIN auth_sessions s ON s.user_id=u.id "
                "WHERE s.token_hash=? AND s.expires_at>?",
                (key, timestamp(self.now())),
            ).fetchone()
        return user(row) if row else None

    def credentials(self, email):
        with self.database.connection() as connection:
            return connection.execute("SELECT * FROM auth_users WHERE email=?", (email,)).fetchone()

    def throttle(self, peer, email):
        now = self.now()
        buckets = [
            (mac(self.settings, "auth-peer", peer), 30),
            (mac(self.settings, "auth-login", peer + ":" + email), 5),
        ]
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM auth_attempts WHERE reset_at<=?", (timestamp(now),))
            # Check both before charging either one; all instances share these counters.
            rows = []
            for key, maximum in buckets:
                row = connection.execute(
                    "SELECT * FROM auth_attempts WHERE key_hash=?", (key,)
                ).fetchone()
                count = row["count"] if row and row["reset_at"] > timestamp(now) else 0
                reset = row["reset_at"] if count else timestamp(now + timedelta(minutes=5))
                if count >= maximum:
                    retry = max(1, int((datetime.fromisoformat(reset) - now).total_seconds()) + 1)
                    raise AuthError("AUTH_RATE_LIMITED", 429, retry_after=retry)
                rows.append((key, count + 1, reset))
            connection.executemany(
                "INSERT INTO auth_attempts VALUES(?,?,?) ON CONFLICT(key_hash) DO UPDATE SET "
                "count=excluded.count,reset_at=excluded.reset_at",
                rows,
            )

    def _issue(self, connection, account, old_token):
        token = secrets.token_urlsafe(32)
        now = self.now()
        if old_token and TOKEN.fullmatch(old_token):
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?", (self.key(old_token),)
            )
        connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (timestamp(now),))
        connection.execute(
            "INSERT INTO auth_sessions VALUES(?,?,?,?)",
            (
                self.key(token),
                account.id,
                timestamp(now),
                timestamp(now + timedelta(seconds=self.settings.session_ttl_seconds)),
            ),
        )
        return account, token

    def register(self, form, password_hash, *, old_token=None):
        account = User(
            id=str(uuid4()),
            email=form.email,
            nickname=form.nickname,
            created_at=timestamp(self.now()),
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO auth_users VALUES(?,?,?,?,?)",
                    (
                        account.id,
                        account.email,
                        account.nickname,
                        password_hash,
                        account.created_at,
                    ),
                )
                return self._issue(connection, account, old_token)
        except sqlite3.IntegrityError:
            if self.credentials(form.email) is not None:
                raise AuthError("ACCOUNT_EXISTS", 409) from None
            raise

    def login(self, observed, *, password_hash=None, old_token=None):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE id=?", (observed["id"],)
            ).fetchone()
            if row is None or row["password_hash"] != observed["password_hash"]:
                raise AuthError("INVALID_CREDENTIALS")
            if password_hash:
                connection.execute(
                    "UPDATE auth_users SET password_hash=? WHERE id=?", (password_hash, row["id"])
                )
            return self._issue(connection, user(row), old_token)

    def logout(self, token):
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self.key(token),))
