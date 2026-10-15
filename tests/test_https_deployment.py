import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import Settings
from app.main import create_app


def test_trusted_proxy_https_origin_secure_cookie_and_forged_forwarding(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            data_dir=tmp_path,
            session_cookie_secure=True,
            session_secret="fixture-https-secret-000000000000000000",
        )
        app = create_app(settings)
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts=["172.22.0.1"])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=wrapped, client=("172.22.0.1", 43210)),
                base_url="https://news.example.com",
            ) as client:
                session = await client.get("/auth/session")
                assert "Secure" in session.headers["set-cookie"]
                assert "HttpOnly" in session.headers["set-cookie"]
                # TLS terminates at the proxy; upstream hop is HTTP with preserved Host.
                request = client.build_request(
                    "POST",
                    "http://news.example.com/auth/register",
                    json={
                        "email": "reader@example.com",
                        "nickname": "reader",
                        "password": "fixture password 42",
                    },
                    headers={
                        "X-Forwarded-Proto": "https",
                        "Origin": "https://news.example.com",
                        "X-CSRF-Token": session.json()["csrf_token"],
                    },
                )
                # Browser sends its Secure cookie to the HTTPS proxy, forwarded upstream.
                request.headers["cookie"] = session.headers["set-cookie"].split(";", 1)[0]
                result = await client.send(request)
                assert result.status_code == 201 and "Secure" in result.headers["set-cookie"]
                request.headers["origin"] = "https://attacker.example.com"
                assert (await client.send(request)).status_code == 403
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=wrapped, client=("203.0.113.9", 43210)),
                base_url="http://news.example.com",
            ) as untrusted:
                response = await untrusted.post(
                    "/auth/register",
                    json={},
                    headers={"X-Forwarded-Proto": "https", "Origin": "https://news.example.com"},
                )
                assert response.status_code == 403

    asyncio.run(scenario())


def test_https_override_uses_exact_proxy_and_keeps_loopback_port(tmp_path):
    root = Path(__file__).resolve().parent.parent
    fake = tmp_path / "fixture.env"
    fake.write_text("SESSION_COOKIE_SECURE=true\n", encoding="utf-8")
    environment = {**os.environ, "NEWS_ENV_FILE": str(fake), "NEWS_TRUSTED_PROXY_IP": "172.22.0.1"}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.https.yaml",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    api = json.loads(result.stdout)["services"]["api"]
    assert api["command"][-2:] == ["--forwarded-allow-ips", "172.22.0.1"]
    assert api["ports"][0]["host_ip"] == "127.0.0.1"
    assert api["environment"]["SESSION_COOKIE_SECURE"] == "true"
    environment.pop("NEWS_TRUSTED_PROXY_IP")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.https.yaml",
            "config",
            "--quiet",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0
