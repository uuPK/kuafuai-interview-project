from contextlib import contextmanager
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.auth import BOOTSTRAP_COOKIE, SESSION_COOKIE, current_user
from app.auth.models import Registration, User
from app.config import Settings
from app.main import create_app
from tests.test_harness import Clock

PASSWORD = "fixture password 42"
FORM = {"email": "Reader@example.com", "nickname": "读者", "password": PASSWORD}


@contextmanager
def client_for(settings):
    app = create_app(settings)

    @app.get("/identity")
    def identity(account: Annotated[User, Depends(current_user)]):
        return {"id": account.id}

    with TestClient(app, base_url="https://testserver") as client:
        client.app.state.auth_store.clock = Clock().now
        yield client


@pytest.fixture
def auth_env(tmp_path):
    config = Settings(
        _env_file=None, data_dir=tmp_path, session_secret="fixture-session-secret-" * 3
    )
    with client_for(config) as client:
        yield client


def protection(client):
    response = client.get("/auth/session")
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def register(client, **changes):
    return client.post("/auth/register", json={**FORM, **changes}, headers=protection(client))


def login(client, **changes):
    return client.post(
        "/auth/login",
        json={k: v for k, v in {**FORM, **changes}.items() if k != "nickname"},
        headers=protection(client),
    )


def test_register_login_rotate_logout_and_private_identity(auth_env, caplog):
    client = auth_env
    assert client.get("/identity").status_code == 401
    created = register(client)
    assert created.status_code == 201
    value = created.json()
    assert value["authenticated"] and value["user"]["email"] == "reader@example.com"
    assert client.get("/identity").json()["id"] == value["user"]["id"]
    old = client.cookies.get(SESSION_COOKIE)
    old_csrf = value["csrf_token"]
    signed = login(client)
    assert signed.status_code == 200 and client.cookies.get(SESSION_COOKIE) != old
    store = client.app.state.auth_store
    assert store.authenticate(old) is None
    assert (
        client.post("/auth/logout", json={}, headers={"X-CSRF-Token": old_csrf}).status_code == 403
    )
    assert client.post("/auth/logout", json={}, headers=protection(client)).status_code == 200
    assert client.get("/identity").status_code == 401
    assert login(client).status_code == 200
    with store.database.connection() as connection:
        hashes = [r["password_hash"] for r in connection.execute("SELECT * FROM auth_users")]
        tokens = [r["token_hash"] for r in connection.execute("SELECT * FROM auth_sessions")]
    assert hashes[0].startswith("$argon2id$")
    assert PASSWORD not in str(hashes) + str(tokens) + caplog.text + created.text
    assert old not in str(tokens) and value["csrf_token"] not in str(tokens)
    assert "password_hash" not in created.text


def test_cookie_flags_cache_and_bootstrap(auth_env):
    response = auth_env.get("/auth/session")
    assert response.json()["authenticated"] is False
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    header = next(
        h for h in response.headers.get_list("set-cookie") if h.startswith(BOOTSTRAP_COOKIE + "=")
    )
    assert "HttpOnly" in header and "SameSite=lax" in header and "Path=/" in header
    created = register(auth_env)
    header = next(
        h for h in created.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE + "=")
    )
    assert "HttpOnly" in header and "Max-Age=86400" in header and "Domain=" not in header
    assert auth_env.cookies.get(BOOTSTRAP_COOKIE) is None


def test_secure_cookie_enabled_and_no_sliding_expiry(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        session_secret="x" * 48,
        session_cookie_secure=True,
        session_ttl_seconds=60,
    )
    with client_for(settings) as client:
        response = register(client)
        assert response.status_code == 201 and "Secure" in response.headers["set-cookie"]
        token = client.cookies.get(SESSION_COOKIE)
        clock = Clock()
        client.app.state.auth_store.clock = clock.now
        clock.advance(59)
        assert client.get("/auth/session").json()["authenticated"]
        clock.advance(1)
        assert client.get("/auth/session").json()["authenticated"] is False
        assert client.app.state.auth_store.authenticate(token) is None


def test_missing_secret_keeps_health_available(tmp_path):
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/auth/session")
        assert (
            response.status_code == 503 and response.json()["error"]["code"] == "AUTH_UNAVAILABLE"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "not-an-email"},
        {"email": "a@example.com\r\nBcc: b@example.com"},
        {"password": "too-short"},
        {"password": "x" * 129},
        {"password": 12345},
        {"nickname": "   "},
        {"nickname": "x\nheader"},
        {"nickname": "x" * 101},
        {"user_id": "other"},
        {"role": "admin"},
        {"email": "用户@example.com"},
    ],
)
def test_invalid_registration_has_no_sensitive_input_echo(auth_env, changes):
    response = register(auth_env, **changes)
    assert response.status_code == 422
    assert PASSWORD not in response.text and '"input":' not in response.text
    assert "password_hash" not in response.text
    with auth_env.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 0


def test_schema_representations_do_not_contain_password():
    value = Registration(**FORM)
    assert PASSWORD not in repr(value) + value.model_dump_json()


@pytest.mark.parametrize(
    "body,content_type,status",
    [
        (b"password=secret", "application/x-www-form-urlencoded", 415),
        (b"{}", "text/plain", 415),
        (b"{", "application/json", 422),
        (b'{"password":"first","password":"second"}', "application/json", 422),
        (b'{"password":NaN}', "application/json", 422),
        (b"[" * 1100 + b"]" * 1100, "application/json", 422),
        (b'{"password":"' + b"x" * 4096 + b'"}', "application/json", 413),
    ],
)
def test_auth_body_boundary(auth_env, body, content_type, status):
    response = auth_env.post(
        "/auth/register",
        content=body,
        headers={"Content-Type": content_type, **protection(auth_env)},
    )
    assert response.status_code == status
    assert "secret" not in response.text and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-CSRF-Token": "bad"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "null"},
        {"Origin": "https://testserver.evil.example"},
        {"Origin": "https://testserver:abc"},
    ],
)
def test_login_csrf_and_origin_are_checked(auth_env, headers):
    valid = protection(auth_env)
    supplied = headers if not headers or "X-CSRF-Token" in headers else {**valid, **headers}
    response = auth_env.post("/auth/register", json=FORM, headers=supplied)
    assert response.status_code == 403


def test_same_origin_is_accepted_and_expired_bootstrap_rejected(auth_env):
    headers = {**protection(auth_env), "Origin": "https://testserver:443"}
    assert auth_env.post("/auth/register", json=FORM, headers=headers).status_code == 201
    assert auth_env.post("/auth/logout", json={}, headers=protection(auth_env)).status_code == 200
    expired = protection(auth_env)
    clock = Clock()
    clock.advance(600)
    auth_env.app.state.auth_store.clock = clock.now
    assert (
        auth_env.post(
            "/auth/login", json={"email": FORM["email"], "password": PASSWORD}, headers=expired
        ).status_code
        == 403
    )


def test_two_users_cannot_use_each_others_csrf(auth_env):
    settings = auth_env.app.state.settings
    first = register(auth_env).json()
    with client_for(settings) as other:
        second = register(other, email="second@example.com").json()
        assert first["user"]["id"] != second["user"]["id"]
        assert other.get("/identity").json()["id"] == second["user"]["id"]
        assert (
            other.post(
                "/auth/logout", json={}, headers={"X-CSRF-Token": first["csrf_token"]}
            ).status_code
            == 403
        )
        assert auth_env.get("/identity").json()["id"] == first["user"]["id"]


def test_bad_password_unknown_account_and_throttle_window(auth_env):
    assert register(auth_env).status_code == 201
    for _ in range(4):
        response = login(auth_env, password="wrong password 000")
        assert (
            response.status_code == 401
            and response.json()["error"]["code"] == "INVALID_CREDENTIALS"
        )
    limited = login(auth_env)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    clock = Clock()
    clock.advance(301)
    auth_env.app.state.auth_store.clock = clock.now
    assert login(auth_env).status_code == 200
    unknown = login(auth_env, email="missing@example.com")
    assert unknown.status_code == 401 and unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_duplicate_email_and_restart_with_rotated_secret(auth_env):
    created = register(auth_env)
    assert register(auth_env, email="READER@example.com").status_code == 409
    old = auth_env.cookies.get(SESSION_COOKIE)
    config = auth_env.app.state.settings
    with client_for(config) as cold:
        cold.cookies.set(SESSION_COOKIE, old)
        assert cold.get("/identity").json()["id"] == created.json()["user"]["id"]
    with client_for(config.model_copy(update={"session_secret": SecretStr("y" * 48)})) as changed:
        changed.cookies.set(SESSION_COOKIE, old)
        assert changed.get("/identity").status_code == 401
        assert login(changed).status_code == 200


def test_session_tampering_logout_requires_auth_and_no_query_csrf(auth_env):
    assert register(auth_env).status_code == 201
    valid = protection(auth_env)["X-CSRF-Token"]
    assert auth_env.post("/auth/logout?csrf_token=" + valid, json={}).status_code == 403
    original = auth_env.cookies.get(SESSION_COOKIE)
    auth_env.cookies.clear()
    auth_env.cookies.set(SESSION_COOKIE, original[:-1] + ("A" if original[-1] != "A" else "B"))
    assert auth_env.get("/identity").status_code == 401
    assert auth_env.post("/auth/logout", json={}, headers=protection(auth_env)).status_code == 401
