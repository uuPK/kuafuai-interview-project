"""Internal Docker health probes; no mutations, credentials, or provider requests."""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from urllib.request import ProxyHandler, build_opener

from app.config import Settings


def api_healthy():
    # Loopback liveness must not inherit a machine's proxy or credentials.
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8000/health", timeout=3) as response:
        return response.status == 200 and json.loads(response.read(1024)) == {"status": "ok"}


def worker_healthy(settings, *, now=None):
    with closing(
        sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True, timeout=2)
    ) as db:
        db.execute("PRAGMA query_only=ON")
        row = db.execute(
            "SELECT heartbeat_at FROM worker_instances WHERE stopped_at IS NULL "
            "ORDER BY heartbeat_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return False
    at = datetime.fromisoformat(row[0])
    if at.tzinfo is None:
        return False
    age = ((now or datetime.now(UTC)) - at).total_seconds()
    threshold = max(
        60,
        settings.request_timeout_seconds * 3 + 30,
        settings.worker_heartbeat_seconds * 4,
        settings.worker_poll_seconds * 3,
    )
    return -5 <= age <= threshold


def main():
    parser = argparse.ArgumentParser(description="内部容器存活检查")
    parser.add_argument("service", choices=("api", "worker"))
    args = parser.parse_args()
    try:
        healthy = (
            api_healthy() if args.service == "api" else worker_healthy(Settings(_env_file=None))
        )
    except Exception:
        healthy = False
    print("healthy" if healthy else "unhealthy")
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
