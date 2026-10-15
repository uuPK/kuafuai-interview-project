import asyncio
import sqlite3
import time

import pytest
from argon2 import PasswordHasher, extract_parameters
from argon2.low_level import Type

import app.storage.database as database_module
from app.auth.models import AuthError, Registration
from app.auth.security import Passwords, csrf, valid_anonymous, valid_csrf
from app.auth.service import AuthService
from app.config import Settings
from app.storage.auth import AuthStore
from app.storage.database import Database
from tests.test_auth import FORM, PASSWORD
from tests.test_harness import Clock


@pytest.fixture
def store(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, session_secret="s" * 48)
    database = Database(settings.database_path)
    database.initialize()
    return AuthStore(database, settings, clock=Clock().now)


def test_argon2id_random_salts_profile_upgrade_and_bad_hash():
    passwords = Passwords()
    first, second = passwords.hash(PASSWORD), passwords.hash(PASSWORD)
    assert first != second and passwords.verify(first, PASSWORD) == (True, None)
    params = extract_parameters(first)
    assert params.type == Type.ID and params.memory_cost == 65536 and params.time_cost == 3
    assert passwords.verify(first, "wrong") == (False, None)
    assert passwords.verify("bad-hash", PASSWORD) == (False, None)
    assert passwords.verify(None, PASSWORD) == (False, None)
    old = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(PASSWORD)
    valid, upgraded = passwords.verify(old, PASSWORD)
    assert valid and upgraded != old and not passwords.hasher.check_needs_rehash(upgraded)


def test_session_creation_failure_rolls_back_new_user(store, monkeypatch):
    def crash(*args):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(store, "_issue", crash)
    with pytest.raises(RuntimeError):
        store.register(Registration(**FORM), Passwords().hash(PASSWORD))
    with store.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 0


def test_password_rehash_and_compare_before_new_session(store):
    old = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(PASSWORD)
    account, token = store.register(Registration(**FORM), old)
    observed = store.credentials(account.email)
    upgraded = Passwords().hash(PASSWORD)
    _, rotated = store.login(observed, password_hash=upgraded, old_token=token)
    assert store.credentials(account.email)["password_hash"] == upgraded
    assert store.authenticate(token) is None and store.authenticate(rotated).id == account.id
    with pytest.raises(AuthError):
        store.login(observed)
    assert store.authenticate("invalid") is None


def test_auth_crypto_concurrency_is_bounded_and_unique_registration_is_atomic(store, monkeypatch):
    async def run():
        service = AuthService(store)
        active = peak = 0
        original = service.passwords.hash

        def measured(password):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.03)
            value = original(password)
            active -= 1
            return value

        monkeypatch.setattr(service.passwords, "hash", measured)
        results = await asyncio.gather(
            *[
                service.register(
                    Registration(**{**FORM, "email": f"user{i}@example.com"}), "peer", None
                )
                for i in range(4)
            ]
        )
        assert len(results) == 4 and peak == 2
        duplicate = await asyncio.gather(
            *[
                service.register(
                    Registration(**{**FORM, "email": "duplicate@example.com"}), "peer", None
                )
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(r, AuthError) for r in duplicate) == 1

    asyncio.run(run())


def test_peer_rate_limit_cannot_be_bypassed_by_changing_email(store):
    for i in range(30):
        store.throttle("one-peer", str(i))
    with pytest.raises(AuthError) as error:
        store.throttle("one-peer", "new")
    assert error.value.status == 429
    store.throttle("other-peer", "new")


def test_csrf_mac_rejects_wrong_format_and_anonymous_timestamps(store):
    now = store.now()
    assert not valid_anonymous("9999999999." + "A" * 43, now)
    assert not valid_anonymous("0." + "A" * 43, now)
    assert not valid_anonymous("invalid", now)
    assert not valid_anonymous(None, now)
    assert not valid_anonymous("A" * 100, now)
    token = "A" * 43
    value = csrf(store.settings, token)
    assert valid_csrf(store.settings, token, value)
    assert not valid_csrf(store.settings, token, value, anonymous=True)
    assert not valid_csrf(store.settings, token, None)


def test_v13_migration_rolls_back_preserving_old_run(tool_env, monkeypatch):
    env = tool_env
    before = env.runs.get_run(env.run.id, "user-1")
    with env.database.connection() as connection:
        for table in (
            "schedule_notices",
            "subscription_schedules",
            "scheduled_runs",
            "subscriptions",
            "auth_sessions",
            "auth_users",
            "auth_attempts",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=12")
    schema = database_module.SCHEMA_V13
    monkeypatch.setattr(database_module, "SCHEMA_V13", (*schema, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        env.database.initialize()
    with env.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='auth_users'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V13", schema)
    env.database.initialize()
    env.database.initialize()
    assert env.runs.get_run(env.run.id, "user-1") == before
