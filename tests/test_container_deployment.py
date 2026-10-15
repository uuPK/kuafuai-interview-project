import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.healthcheck import api_healthy, worker_healthy
from app.tools.bash import DockerRunner
from app.tools.models import ToolFailure
from scripts.build_images import prepare_context
from tests.test_files import context


def test_named_volume_mount_is_only_the_current_run(material_env):
    env = material_env
    config = env.settings.model_copy(update={"bash_data_volume": "fixture-project-data"})
    runner = DockerRunner(config)
    _, args = runner.arguments(context(env), ["/usr/bin/ls", "/work"], "test", Path("config"))
    assert args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == (
        f"type=volume,src=fixture-project-data,dst=/work,readonly,volume-subpath=runs/{env.run.id}"
    )
    assert "--network=none" in args and "--read-only" in args
    assert not any("docker.sock" in value for value in args[args.index("run") :])
    wrong = SimpleNamespace(working_dir=env.root.parent, run_id=env.run.id)
    with pytest.raises(ToolFailure, match="BASH_CONFIGURATION"):
        runner.arguments(wrong, [], "test", Path("config"))
    wrong = SimpleNamespace(working_dir=env.root, run_id="../another")
    with pytest.raises(ToolFailure, match="BASH_CONFIGURATION"):
        runner.arguments(wrong, [], "test", Path("config"))


def test_build_context_excludes_credentials_runtime_and_bytecode(tmp_path):
    project, target = tmp_path / "project", tmp_path / "context"
    project.mkdir()
    target.mkdir()
    for name in (".dockerignore", "pyproject.toml", "uv.lock"):
        (project / name).write_text("fixture", encoding="utf-8")
    (project / ".env").write_text("PRIVATE=fixture-private-secret", encoding="utf-8")
    for name in ("app", "docker", "data", ".git"):
        (project / name).mkdir()
        (project / name / "file.txt").write_text(name, encoding="utf-8")
    (project / "app" / ".env").write_text("PRIVATE=fixture-nested-secret", encoding="utf-8")
    (project / "app" / "module.pyc").write_bytes(b"stale")
    prepare_context(project, target)
    assert (target / "app" / "file.txt").exists()
    assert not (target / ".env").exists() and not (target / "data").exists()
    assert not (target / ".git").exists() and not (target / "app" / ".env").exists()
    assert not (target / "app" / "module.pyc").exists()


@pytest.mark.parametrize(
    "volume", ["../other", "name,src=/", "x y", "/host", "name:tag", "x" * 129]
)
def test_invalid_volume_configuration_is_rejected(volume):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bash_data_volume=volume)


@pytest.mark.parametrize(
    "age,healthy", [(-6, False), (-5, True), (0, True), (120, True), (121, False)]
)
def test_worker_probe_uses_recent_persisted_heartbeat(tmp_path, age, healthy):
    config = Settings(_env_file=None, data_dir=tmp_path)
    at = datetime(2026, 10, 15, tzinfo=UTC)
    with closing(sqlite3.connect(config.database_path, autocommit=True)) as db:
        db.execute("CREATE TABLE worker_instances(id TEXT,heartbeat_at TEXT,stopped_at TEXT)")
        db.execute(
            "INSERT INTO worker_instances VALUES('worker',?,NULL)",
            ((at - timedelta(seconds=age)).isoformat(),),
        )
    assert worker_healthy(config, now=at) is healthy
    with closing(sqlite3.connect(config.database_path, autocommit=True)) as db:
        db.execute("UPDATE worker_instances SET stopped_at='stopped'")
    assert worker_healthy(config, now=at) is False


def test_worker_probe_does_not_create_missing_database(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        worker_healthy(config)
    assert not config.database_path.exists()


@pytest.mark.parametrize("stamp", ["broken", "2026-10-15T00:00:00", None])
def test_worker_probe_rejects_invalid_heartbeat(tmp_path, stamp):
    config = Settings(_env_file=None, data_dir=tmp_path)
    with closing(sqlite3.connect(config.database_path, autocommit=True)) as db:
        db.execute("CREATE TABLE worker_instances(id TEXT,heartbeat_at TEXT,stopped_at TEXT)")
        db.execute("INSERT INTO worker_instances VALUES('worker',?,NULL)", (stamp,))
    if stamp and "T" in stamp:
        assert not worker_healthy(config)
    else:
        with pytest.raises((TypeError, ValueError)):
            worker_healthy(config)


def test_probe_command_fails_without_exposing_internal_error(monkeypatch, capsys):
    from app import healthcheck

    monkeypatch.setattr("sys.argv", ["healthcheck", "api"])

    def failed():
        raise RuntimeError("fixture-private-credential")

    monkeypatch.setattr(healthcheck, "api_healthy", failed)
    with pytest.raises(SystemExit) as result:
        healthcheck.main()
    assert result.value.code == 1 and capsys.readouterr().out == "unhealthy\n"


def test_api_probe_ignores_proxy_and_requires_exact_health_payload(monkeypatch):
    import app.healthcheck as healthcheck

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, maximum):
            assert maximum == 1024
            return b'{"status":"ok"}'

    def opener(handler):
        assert handler.proxies == {}

        def open_url(url, timeout):
            assert url == "http://127.0.0.1:8000/health" and timeout == 3
            return Response()

        return SimpleNamespace(open=open_url)

    monkeypatch.setattr(healthcheck, "build_opener", opener)
    assert api_healthy()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI unavailable")
def test_compose_expanded_contract_has_separate_permissions_and_shared_storage(
    tmp_path, monkeypatch
):
    project = Path(__file__).resolve().parent.parent
    shutil.copyfile(project / "compose.yaml", tmp_path / "compose.yaml")
    (tmp_path / ".env").write_text(
        "SESSION_SECRET=fixture-compose-secret-000000000000000000\n", encoding="utf-8"
    )
    for key in ("NEWS_ENV_FILE", "NEWS_HTTP_PORT", "BASH_SOCKET_GID", "COMPOSE_PROJECT_NAME"):
        monkeypatch.delenv(key, raising=False)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "fixture-deploy",
            "--profile",
            "build",
            "-f",
            str(tmp_path / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    api, worker = config["services"]["api"], config["services"]["worker"]
    assert api["image"] == worker["image"]
    assert api["ports"][0]["host_ip"] == "127.0.0.1"
    assert all(v["source"] != "/var/run/docker.sock" for v in api["volumes"])
    assert any(v["source"] == "/var/run/docker.sock" for v in worker["volumes"])
    assert worker["environment"]["BASH_DATA_VOLUME"] == "fixture-deploy-data"
    assert config["volumes"]["news-data"]["name"] == "fixture-deploy-data"
    for service in (api, worker):
        assert service["user"] == "10001:10001" and service["read_only"]
        assert service["restart"] == "unless-stopped" and service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
    assert worker["healthcheck"]["test"][-1] == "worker"
    assert config["services"]["tools"]["network_mode"] == "none"
