"""One live subscription per account; preview creation uses a fixed atomic snapshot."""

import sqlite3
from uuid import uuid4

from app.auth.models import AuthError
from app.harness.redaction import Redactor
from app.product.models import ProductError, ProfileInput, SubscriptionInput
from app.storage.auth import user
from app.storage.models import RunRequest, SubscriptionSnapshot, timestamp
from app.storage.schedules import ScheduleStore
from app.tools.models import ToolFailure


class SubscriptionStore:
    def __init__(self, runs, settings):
        self.runs, self.database = runs, runs.database
        self.redactor = Redactor(settings)

    def _safe(self, form):
        values = form.model_dump(mode="json")
        if self.redactor.clean(values) != values:
            raise ProductError("SENSITIVE_INPUT", 422)

    @staticmethod
    def _account(connection, owner):
        row = connection.execute("SELECT * FROM auth_users WHERE id=?", (owner,)).fetchone()
        if row is None:
            raise AuthError("AUTH_REQUIRED")
        return user(row)

    @staticmethod
    def _record(row):
        try:
            form = SubscriptionInput.model_validate_json(row["preferences_json"])
            return {
                "id": row["id"],
                **form.model_dump(),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        except ValueError:
            raise ToolFailure("SUBSCRIPTION_STATE_PROBLEM", "订阅记录损坏，请核查存储。") from None

    def get(self, owner):
        with self.database.connection() as connection:
            self._account(connection, owner)
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (owner,)
            ).fetchone()
            result = self._record(row) if row else None
            if result:
                result["schedule"] = ScheduleStore.summary(connection, result["id"])
            return result

    def save(self, owner, form):
        form = SubscriptionInput.model_validate(form)
        self._safe(form)
        at = timestamp(self.runs.now())
        with self.database.transaction() as connection:
            self._account(connection, owner)
            previous = connection.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (owner,)
            ).fetchone()
            connection.execute(
                "INSERT INTO subscriptions VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "preferences_json=excluded.preferences_json,updated_at=excluded.updated_at",
                (str(uuid4()), owner, form.model_dump_json(), at, at),
            )
            result = self._record(
                connection.execute(
                    "SELECT * FROM subscriptions WHERE user_id=?", (owner,)
                ).fetchone()
            )
            try:
                before = self._record(previous) if previous else None
            except ToolFailure:
                before = None
            ScheduleStore(self.runs, self).changed(connection, before, result, self.runs.now())
            result["schedule"] = ScheduleStore.summary(connection, result["id"])
            return result

    def profile(self, owner, form):
        form = ProfileInput.model_validate(form)
        self._safe(form)
        try:
            with self.database.transaction() as connection:
                self._account(connection, owner)
                connection.execute(
                    "UPDATE auth_users SET email=?,nickname=? WHERE id=?",
                    (form.email, form.nickname, owner),
                )
                return self._account(connection, owner)
        except sqlite3.IntegrityError:
            with self.database.connection() as connection:
                duplicate = connection.execute(
                    "SELECT 1 FROM auth_users WHERE email=? AND id<>?", (form.email, owner)
                ).fetchone()
            if duplicate:
                raise ProductError("ACCOUNT_EXISTS", 409) from None
            raise

    def preview(self, owner):
        with self.database.transaction() as connection:
            account = self._account(connection, owner)
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (owner,)
            ).fetchone()
            if row is None:
                raise ProductError("SUBSCRIPTION_REQUIRED")
            saved = self._record(row)
            if not saved["enabled"]:
                raise ProductError("SUBSCRIPTION_DISABLED")
            existing = connection.execute(
                "SELECT id FROM runs WHERE user_id=? AND mode='preview' "
                "AND status IN ('queued','running','waiting_retry') ORDER BY created_at,id LIMIT 1",
                (owner,),
            ).fetchone()
            if existing:
                return existing["id"], True
            snapshot = SubscriptionSnapshot(
                nickname=account.nickname,
                email=account.email,
                **{key: saved[key] for key in SubscriptionInput.model_fields},
            )
            self._safe(snapshot)
            run = self.runs._create_run(
                connection,
                RunRequest(user_id=owner, subscription_id=saved["id"], subscription=snapshot),
                self.runs.now(),
            )
            return run.id, False

    def delivery_enabled(self, connection, run):
        row = connection.execute(
            "SELECT * FROM subscriptions WHERE id=? AND user_id=?",
            (run.subscription_id, run.user_id),
        ).fetchone()
        # Missing live configuration cannot authorize delivery of a historical/manual run.
        return self._record(row)["enabled"] if row else False
