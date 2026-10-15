"""Atomically claim queued/due/orphaned work, with renewable owner capabilities."""

import json
from datetime import timedelta
from uuid import uuid4

from pydantic import AwareDatetime, Field

from app.harness.execution import ExecutionClaim, LeaseLostError
from app.storage.models import StoredModel, timestamp


class RunLease(StoredModel):
    run_id: str
    user_id: str
    worker_id: str = Field(min_length=1, max_length=128)
    token: str = Field(pattern=r"^[a-f0-9]{32}$", repr=False)
    expires_at: AwareDatetime


class LeaseStore:
    def __init__(self, runs, settings):
        self.runs, self.database, self.settings = runs, runs.database, settings

    def touch_worker(self, worker_id):
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise ValueError("invalid worker identity")
        at = timestamp(self.runs.now())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO worker_instances VALUES(?,?,?,NULL) ON CONFLICT(id) "
                "DO UPDATE SET heartbeat_at=excluded.heartbeat_at, stopped_at=NULL",
                (worker_id, at, at),
            )

    def claim_next(self, worker_id):
        at = self.runs.now()
        with self.database.transaction() as connection:
            worker = connection.execute(
                "SELECT * FROM worker_instances WHERE id=?", (worker_id,)
            ).fetchone()
            if worker is None or worker["stopped_at"] is not None:
                raise ValueError("worker must be registered")
            row = connection.execute(
                """SELECT r.*, l.token AS old_token FROM runs r
                LEFT JOIN run_leases l ON l.run_id=r.id
                WHERE (l.run_id IS NULL OR l.expires_at<=?) AND (
                    r.status IN ('queued','running') OR
                    (r.status='waiting_retry' AND (r.retry_at<=? OR r.deadline_at<=?)) OR
                    EXISTS(SELECT 1 FROM model_tool_calls m LEFT JOIN run_messages f
                        ON f.run_id=m.run_id AND f.tool_call_id=m.tool_call_id AND f.role='tool'
                        WHERE m.run_id=r.id AND f.seq IS NULL)
                        AND r.status IN ('completed','no_news','failed') OR
                    r.status IN ('completed','no_news','failed') AND EXISTS(
                        SELECT 1 FROM external_requests e
                        WHERE e.run_id=r.id AND e.status!='completed') OR
                    l.run_id IS NOT NULL AND r.status IN ('completed','no_news','failed')
                ) ORDER BY CASE WHEN r.status='queued' THEN 1 ELSE 0 END,
                    r.created_at, r.id LIMIT 1""",
                (timestamp(at), timestamp(at), timestamp(at)),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            expires = at + timedelta(seconds=self.settings.worker_lease_seconds)
            connection.execute(
                "INSERT INTO run_leases VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                "worker_id=excluded.worker_id,token=excluded.token,heartbeat_at=excluded.heartbeat_at,"
                "expires_at=excluded.expires_at",
                (row["id"], worker_id, token, timestamp(at), timestamp(expires)),
            )
            # This trusted ownership transaction installs the capability before entering its scope.
            connection.execute(
                "INSERT INTO run_events(run_id,kind,occurred_at,data_json) VALUES(?,?,?,?)",
                (
                    row["id"],
                    "worker_reclaimed"
                    if row["old_token"] or row["status"] != "queued"
                    else "worker_claimed",
                    timestamp(at),
                    json.dumps({"worker_id": worker_id}),
                ),
            )
            return RunLease(
                run_id=row["id"],
                user_id=row["user_id"],
                worker_id=worker_id,
                token=token,
                expires_at=expires,
            )

    def heartbeat(self, lease):
        at = self.runs.now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE run_leases SET heartbeat_at=?,expires_at=? WHERE run_id=? "
                "AND worker_id=? AND token=? AND expires_at>?",
                (
                    timestamp(at),
                    timestamp(at + timedelta(seconds=self.settings.worker_lease_seconds)),
                    lease.run_id,
                    lease.worker_id,
                    lease.token,
                    timestamp(at),
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("cannot renew a replaced or expired claim")
            connection.execute(
                "UPDATE worker_instances SET heartbeat_at=? WHERE id=? AND stopped_at IS NULL",
                (timestamp(at), lease.worker_id),
            )

    def release(self, lease):
        with self.database.transaction() as connection:
            return (
                connection.execute(
                    "DELETE FROM run_leases WHERE run_id=? AND token=?", (lease.run_id, lease.token)
                ).rowcount
                == 1
            )

    def stop_worker(self, worker_id):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE worker_instances SET stopped_at=? WHERE id=?",
                (timestamp(self.runs.now()), worker_id),
            )

    def execution(self, lease):
        return ExecutionClaim(self.database, lease.run_id, lease.token, self.runs.now)
