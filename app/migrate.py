"""Explicit entry point for the same migrations applied by API/Worker startup."""

import argparse
import sqlite3
from pathlib import Path

from app.config import Settings
from app.storage.database import Database, SchemaVersionError
from app.storage.schema import SCHEMA_VERSION


def main(argv=None):
    parser = argparse.ArgumentParser(description="Initialize or upgrade the SQLite schema")
    parser.add_argument("--data-dir", type=Path, help="Override DATA_DIR from environment/.env")
    args = parser.parse_args(argv)
    try:
        overrides = {"data_dir": args.data_dir} if args.data_dir else {}
        database = Database(Settings(**overrides).database_path)
        database.initialize()
    except SchemaVersionError:
        parser.exit(1, "Database uses a newer schema; use the matching application version.\n")
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Migration failed; check configuration, storage permissions and database.\n")
    print(f"Database ready: schema version {SCHEMA_VERSION}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
