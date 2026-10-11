import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

import app.storage.database as database_module
from app.storage.database import Database, SchemaVersionError


def test_initialize_is_idempotent_and_connections_are_configured(tmp_path: Path) -> None:
    database = Database(tmp_path / "nested" / "news.sqlite3", busy_timeout_ms=1234)
    assert not database.path.exists()
    database.initialize()
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } >= {"runs", "run_events"}
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("timeout", [0, -1, 60001, True, "5000", 1.5])
def test_invalid_busy_timeout_is_rejected(tmp_path: Path, timeout: object) -> None:
    with pytest.raises(ValueError):
        Database(tmp_path / "news.sqlite3", busy_timeout_ms=timeout)


def test_future_schema_is_rejected_without_downgrading(tmp_path: Path) -> None:
    database = Database(tmp_path / "news.sqlite3")
    database.initialize()
    with database.connection() as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaVersionError):
        database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_schema_creation_rolls_back_every_statement(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "news.sqlite3")
    monkeypatch.setattr(
        database_module, "SCHEMA_V1", ("CREATE TABLE incomplete (id INTEGER)", "INVALID SQL")
    )
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'incomplete'"
            ).fetchone()
            is None
        )


def test_transaction_commit_and_rollback_including_interrupt(tmp_path: Path) -> None:
    database = Database(tmp_path / "news.sqlite3")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("CREATE TABLE transaction_probe (value INTEGER)")
        connection.execute("INSERT INTO transaction_probe VALUES (1)")
    with pytest.raises(KeyboardInterrupt), database.transaction() as connection:
        connection.execute("INSERT INTO transaction_probe VALUES (2)")
        raise KeyboardInterrupt
    with database.connection() as connection:
        assert [row[0] for row in connection.execute("SELECT value FROM transaction_probe")] == [1]


def test_foreign_key_rejects_orphan_event(tmp_path: Path) -> None:
    database = Database(tmp_path / "news.sqlite3")
    database.initialize()
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            "INSERT INTO run_events (run_id, kind, occurred_at, data_json) VALUES (?, ?, ?, ?)",
            ("unknown-run", "example", "2026-10-11T01:00:00+00:00", "{}"),
        )


def test_initialization_can_be_shared_by_api_and_worker(tmp_path: Path) -> None:
    path = tmp_path / "news.sqlite3"
    Database(path).initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: Database(path).initialize(), range(2)))
    assert results == [None, None]


def test_non_wal_storage_is_rejected(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "news.sqlite3")

    @contextmanager
    def in_memory_connection():
        with closing(sqlite3.connect(":memory:")) as connection:
            yield connection

    monkeypatch.setattr(database, "connection", in_memory_connection)
    with pytest.raises(RuntimeError, match="WAL"):
        database.initialize()
