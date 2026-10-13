"""SQLite configuration and explicit, short transactions without shared connections."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.storage.schema import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V10,
    SCHEMA_VERSION,
)


class SchemaVersionError(RuntimeError):
    """This application cannot read a database created by a newer schema."""


class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be an integer")
        if not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000, autocommit=True
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writes; rollback state and its timeline together on any failure."""
        with self.connection() as connection, self._transaction(connection):
            yield connection

    def initialize(self) -> None:
        """Create schema atomically, preserve existing records, reject future versions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("database requires WAL journal mode")
            with self._transaction(connection):
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise SchemaVersionError("database schema is newer than this application")
                if version == 0:
                    for statement in SCHEMA_V1:
                        connection.execute(statement)
                    version = 1
                if version == 1:
                    for statement in SCHEMA_V2:
                        connection.execute(statement)
                    version = 2
                if version == 2:
                    for statement in SCHEMA_V3:
                        connection.execute(statement)
                    version = 3
                if version == 3:
                    for statement in SCHEMA_V4:
                        connection.execute(statement)
                    version = 4
                if version == 4:
                    for statement in SCHEMA_V5:
                        connection.execute(statement)
                    version = 5
                if version == 5:
                    for statement in SCHEMA_V6:
                        connection.execute(statement)
                    version = 6
                if version == 6:
                    for statement in SCHEMA_V7:
                        connection.execute(statement)
                    version = 7
                if version == 7:
                    for statement in SCHEMA_V8:
                        connection.execute(statement)
                    version = 8
                if version == 8:
                    for statement in SCHEMA_V9:
                        connection.execute(statement)
                    version = 9
                if version == 9:
                    for statement in SCHEMA_V10:
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
