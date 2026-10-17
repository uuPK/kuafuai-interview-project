"""Opt-in real Compose deployment; isolated project, fake secrets, own volume only."""

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_COMPOSE_LIVE") != "1",
    reason="set NEWS_COMPOSE_LIVE=1 for real Compose acceptance",
)


def test_real_compose_health_restart_persistence_and_worker_bash(tmp_path):
    root = Path(__file__).resolve().parent.parent
    project = "news-accept-" + uuid4().hex[:12]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    envfile = tmp_path / "compose.env"
    envfile.write_text(
        "SESSION_SECRET=fixture-compose-secret-000000000000000000\nSMTP_ENABLED=false\nDEEPSEEK_API_KEY=fixture-compose-private-key\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "NEWS_ENV_FILE": str(envfile), "NEWS_HTTP_PORT": str(port)}

    def docker(*args, timeout=120, check=True):
        result = subprocess.run(
            ["docker", *args],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if check:
            assert result.returncode == 0, result.stderr[-5000:]
        return result.stdout

    def compose(*args, **kwargs):
        return docker("compose", "-p", project, *args, **kwargs)

    report = {
        "project": project,
        "mode": "production image and API/Worker with fake credentials; no model/mail calls",
    }
    restored_project = project + "-restore"
    try:
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "api", "worker")
        containers = {
            service: compose("ps", "-q", service).strip() for service in ("api", "worker")
        }
        for service, identifier in containers.items():
            record = json.loads(docker("inspect", identifier))[0]
            assert record["State"]["Health"]["Status"] == "healthy"
            assert record["Config"]["User"] == "10001:10001"
            assert record["HostConfig"]["ReadonlyRootfs"] and not record["HostConfig"]["Privileged"]
            assert record["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
            sockets = [m for m in record["Mounts"] if m["Destination"] == "/var/run/docker.sock"]
            assert len(sockets) == (1 if service == "worker" else 0)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as api:
            assert api.get("/").status_code == 200 and api.get("/assets/app.js").status_code == 200
            assert api.get("/assets/favicon.svg").status_code == 200
            assert api.get("/favicon.ico").headers["content-type"] == "image/svg+xml"
            csrf = api.get("/auth/session").json()["csrf_token"]
            registered = api.post(
                "/auth/register",
                json={
                    "email": "reader@example.com",
                    "nickname": "fixture",
                    "password": "fixture password 42",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert registered.status_code == 201
            owner = registered.json()["user"]["id"]
            csrf = registered.json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            assert (
                api.put(
                    "/subscription", json={"topics": ["AI"], "push_time": "23:59"}, headers=headers
                ).status_code
                == 200
            )
            run_id = api.post("/runs", json={}, headers=headers).json()["run_id"]
            for _ in range(60):
                run = api.get("/runs/" + run_id).json()
                if run["status"] == "failed":
                    break
                time.sleep(0.25)
            assert run["stop_reason"] == "model_dependency_configuration"
            assert api.get("/briefs").json()["items"] == []
            # Signal the application through init; process exit exercises the restart policy.
            docker(
                "exec",
                containers["worker"],
                "python",
                "-c",
                "import os,signal;os.kill(1,signal.SIGTERM)",
            )
            for _ in range(90):
                record = json.loads(docker("inspect", containers["worker"]))[0]
                if (
                    record["RestartCount"] >= 1
                    and record["State"].get("Health", {}).get("Status") == "healthy"
                ):
                    break
                time.sleep(0.5)
            assert record["RestartCount"] >= 1 and record["State"]["Health"]["Status"] == "healthy"
            compose("restart", "api")
            for _ in range(60):
                try:
                    if api.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            assert api.get("/subscription").json()["subscription"]["topics"] == ["AI"]
            assert (
                api.get("/runs/" + run_id).json()["stop_reason"] == "model_dependency_configuration"
            )
        compose("stop", "worker")
        program = (root / "tests/compose_probe.py").read_text(encoding="utf-8")
        output = compose(
            "run", "--rm", "--no-deps", "-T", "worker", "python", "-c", program, owner, timeout=60
        )
        probe = json.loads(
            next(
                line.removeprefix("COMPOSE_PROBE ")
                for line in output.splitlines()
                if line.startswith("COMPOSE_PROBE ")
            )
        )
        assert probe["bash"] == probe["isolation"] == "passed"
        # Stop every writer before taking the DB/files snapshot. Restore to a second volume.
        compose("stop", "api", "worker")
        backups = tmp_path / "backups"
        backups.mkdir()
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-v",
            f"{backups}:/backups",
            "api",
            "python",
            "-m",
            "app.ops",
            "backup",
            "--data-dir",
            "/data",
            "--archive",
            "/backups/data.zip",
            "--writers-stopped",
        )
        docker(
            "compose",
            "-p",
            restored_project,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-v",
            f"{backups}:/backups:ro",
            "api",
            "python",
            "-m",
            "app.ops",
            "restore",
            "--data-dir",
            "/data",
            "--archive",
            "/backups/data.zip",
            "--writers-stopped",
        )
        docker(
            "compose",
            "-p",
            restored_project,
            "up",
            "-d",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "90",
            "api",
            "worker",
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
        ) as restored:
            # Same server-side session and subscription survive restoring the whole volume.
            restored.cookies.update(api.cookies)
            assert restored.get("/subscription").json()["subscription"]["topics"] == ["AI"]
            assert (
                restored.get("/runs/" + run_id).json()["stop_reason"]
                == "model_dependency_configuration"
            )
        assert (
            docker(
                "compose",
                "-p",
                restored_project,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from pathlib import Path; assert list(Path('/data/runs').glob('*/scratch/*'))",
            )
            is not None
        )
        report["backup_restore"] = (
            "passed: stopped writers, new volume, healthy API/Worker and original session/data"
        )
        report.update(health="passed", restart="passed", persistence="passed", probe=probe)
        target = root / "data/acceptance/compose-step-5.1.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        for name in (restored_project, project):
            docker("compose", "-p", name, "down", "--remove-orphans", check=False)
            # Only these new, labelled test projects' volumes may be removed.
            volume = name + "-data"
            raw = docker("volume", "inspect", volume, check=False)
            if raw:
                saved = json.loads(raw)[0]
                assert saved["Labels"]["com.docker.compose.project"] == name
                docker("volume", "rm", volume)
