"""The README configuration and database commands work on a fresh directory."""

import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from contextlib import closing
from pathlib import Path

import pytest

from app.config import Settings
from app.migrate import main as migrate
from app.storage.database import Database
from app.storage.schema import SCHEMA_VERSION
from scripts.prepare_env import main as prepare_main
from scripts.prepare_env import prepare

ROOT = Path(__file__).resolve().parent.parent


def example(directory):
    (directory / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())


def test_generated_config_has_unique_secret_and_remains_private(tmp_path, capsys):
    example(tmp_path)
    assert prepare_main(["--directory", str(tmp_path)]) == 0
    config = Settings(_env_file=tmp_path / ".env", data_dir=tmp_path)
    secret = config.session_secret.get_secret_value()
    assert len(secret) >= 32
    assert config.rss_enabled is False and config.smtp_enabled is False
    assert secret not in capsys.readouterr().out
    saved = (tmp_path / ".env").read_bytes()
    assert prepare(tmp_path) is False
    assert (tmp_path / ".env").read_bytes() == saved
    other = tmp_path / "other"
    other.mkdir()
    example(other)
    assert prepare(other)
    assert Settings(_env_file=other / ".env").session_secret.get_secret_value() != secret


def test_existing_env_is_preserved_even_without_example(tmp_path):
    saved = b"SESSION_SECRET=existing-private-value\r\nSMTP_PASSWORD=existing-private-password"
    (tmp_path / ".env").write_bytes(saved)
    assert prepare(tmp_path) is False
    assert (tmp_path / ".env").read_bytes() == saved


def test_concurrent_env_creation_preserves_the_other_writer(tmp_path, monkeypatch):
    import scripts.prepare_env as module

    example(tmp_path)

    def other_writer(_size):
        (tmp_path / ".env").write_text("existing private configuration", encoding="utf-8")
        return "unused-random-secret"

    monkeypatch.setattr(module.secrets, "token_urlsafe", other_writer)
    assert prepare(tmp_path) is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "existing private configuration"


@pytest.mark.parametrize("template", [None, "SESSION_SECRET=not-empty\n"])
def test_invalid_template_never_creates_a_partial_env(tmp_path, template, capsys):
    if template is not None:
        (tmp_path / ".env.example").write_text(template, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        prepare_main(["--directory", str(tmp_path)])
    assert error.value.code == 1 and not (tmp_path / ".env").exists()
    assert "Cannot create" in capsys.readouterr().err


def test_migration_cli_creates_tables_and_preserves_existing_records(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert migrate(["--data-dir", str(tmp_path)]) == 0
    database = Database(tmp_path / "news.sqlite3")
    with database.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables >= {"auth_users", "subscriptions", "runs", "briefs", "mail_outbox"}
        connection.execute("CREATE TABLE quickstart_marker (value TEXT)")
        connection.execute("INSERT INTO quickstart_marker VALUES ('preserved')")
    assert migrate(["--data-dir", str(tmp_path)]) == 0
    with database.connection() as connection:
        assert (
            connection.execute("SELECT value FROM quickstart_marker").fetchone()[0] == "preserved"
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "Database ready" in capsys.readouterr().out


def test_migration_uses_environment_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "chosen"))
    assert migrate([]) == 0
    assert (tmp_path / "chosen/news.sqlite3").is_file()


@pytest.mark.parametrize("failure", ["future", "invalid_configuration", "invalid_database"])
def test_migration_failures_return_controlled_error(tmp_path, monkeypatch, capsys, failure):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "news.sqlite3"
    if failure == "future":
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    elif failure == "invalid_database":
        path.write_bytes(b"not a database")
    else:
        monkeypatch.setenv("SESSION_SECRET", "private-credential-must-not-appear")
        monkeypatch.setenv("SMTP_PORT", "invalid")
    with pytest.raises(SystemExit) as error:
        migrate(["--data-dir", str(tmp_path)])
    assert error.value.code == 1
    assert "private-credential" not in capsys.readouterr().err
    if failure == "future":
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_readme_entry_points_execute_as_commands(tmp_path):
    example(tmp_path)
    prepared = subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_env.py"), "--directory", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Created .env" in prepared.stdout
    migrated = subprocess.run(
        [sys.executable, "-m", "app.migrate", "--data-dir", str(tmp_path)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"schema version {SCHEMA_VERSION}" in migrated.stdout


def test_readme_local_links_and_database_version_match_repository():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", readme):
        if "://" not in target:
            assert (ROOT / target.split("#")[0]).is_file(), target
    assert f"schema version {SCHEMA_VERSION}." in readme


def test_requirements_are_pinned_to_the_project_lockfile():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    locked = {(package["name"], package["version"]) for package in lock["package"]}
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pinned = set(re.findall(r"^([a-z0-9-]+)==([^\s;]+)", requirements, re.MULTILINE))
    direct = {re.split(r"[<>=\[ ]", value)[0] for value in project["project"]["dependencies"]}
    assert direct <= {name for name, _ in pinned}
    assert pinned <= locked
    assert requirements.count("--hash=sha256:") >= len(pinned)
