"""Outbox send claims, bounded retries, unknown recovery, and explicit owner resend."""

from datetime import timedelta
from uuid import uuid4

from app.harness.execution import LeaseLostError
from app.product.models import ProductError
from app.providers.smtp import SendOutcome
from app.storage.models import timestamp
from app.storage.runs import _record
from app.tools.models import ToolFailure


class DispatchStore:
    def __init__(self, outbox, briefs, settings):
        self.outbox, self.briefs, self.settings = outbox, briefs, settings
        self.runs, self.database = outbox.runs, outbox.database

    def _meta(self, connection, row):
        connection.execute(
            "INSERT OR IGNORE INTO mail_dispatch VALUES(?,NULL,NULL,NULL,NULL,0,?)",
            (
                row["brief_id"],
                f"<{row['brief_id']}.{row['payload_hash'][:16]}@daily-ai.invalid>",
            ),
        )
        return connection.execute(
            "SELECT * FROM mail_dispatch WHERE brief_id=?", (row["brief_id"],)
        ).fetchone()

    def _event(self, connection, row, kind, data):
        self.runs._append_event(
            connection, row["run_id"], kind, self.runs.now(), {"brief_id": row["brief_id"], **data}
        )

    def _unknown(self, connection, row):
        meta = self._meta(connection, row)
        at = timestamp(self.runs.now())
        connection.execute(
            "UPDATE mail_outbox SET status='unknown',updated_at=? WHERE brief_id=?",
            (at, row["brief_id"]),
        )
        connection.execute(
            "UPDATE mail_dispatch SET token=NULL,expires_at=NULL,next_at=NULL,"
            "error_code='SMTP_RESULT_UNKNOWN' WHERE brief_id=?",
            (row["brief_id"],),
        )
        connection.execute(
            "UPDATE mail_attempts SET status='unknown',finished_at=?,"
            "error_code='SMTP_RESULT_UNKNOWN' WHERE token=? AND status='sending'",
            (at, meta["token"]),
        )
        self._event(
            connection,
            row,
            "mail_unknown",
            {"status": "unknown", "error_code": "SMTP_RESULT_UNKNOWN"},
        )

    def recover_expired(self):
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT m.* FROM mail_outbox m LEFT JOIN mail_dispatch d ON d.brief_id=m.brief_id "
                "WHERE m.status='sending' AND (d.expires_at IS NULL OR d.expires_at<=?)",
                (timestamp(self.runs.now()),),
            ).fetchall()
            for row in rows:
                self._unknown(connection, row)
        return len(rows)

    def claim(self):
        at = self.runs.now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT m.* FROM mail_outbox m LEFT JOIN mail_dispatch d ON d.brief_id=m.brief_id "
                "WHERE m.status='pending' AND (d.next_at IS NULL OR d.next_at<=?) "
                "ORDER BY m.created_at,m.brief_id LIMIT 20",
                (timestamp(at),),
            ).fetchall()
            for row in rows:
                meta = self._meta(connection, row)
                try:
                    record = self.outbox._record(row)
                    run = _record(
                        self.runs._owned_row(
                            connection,
                            row["run_id"],
                            connection.execute(
                                "SELECT user_id FROM runs WHERE id=?", (row["run_id"],)
                            ).fetchone()[0],
                        )
                    )
                    enabled = self.outbox.delivery_enabled(connection, run)
                    if type(enabled) is not bool:
                        raise ToolFailure("MAIL_STATE_PROBLEM", "订阅资格状态无效。")
                    code = (
                        "SUBSCRIPTION_DISABLED"
                        if not run.subscription.enabled or not enabled
                        else "MAIL_RETRY_EXHAUSTED"
                        if meta["cycle_attempts"] >= self.settings.mail_max_attempts
                        else None
                    )
                except (ToolFailure, ValueError, TypeError):
                    code = "MAIL_STATE_PROBLEM"
                if code:
                    connection.execute(
                        "UPDATE mail_outbox SET status='failed',updated_at=? WHERE brief_id=?",
                        (timestamp(at), row["brief_id"]),
                    )
                    connection.execute(
                        "UPDATE mail_dispatch SET error_code=?,next_at=NULL WHERE brief_id=?",
                        (code, row["brief_id"]),
                    )
                    self._event(
                        connection, row, "mail_failed", {"status": "failed", "error_code": code}
                    )
                    continue
                token = uuid4().hex
                expiry = at + timedelta(
                    seconds=max(60, self.settings.request_timeout_seconds * 3 + 30)
                )
                connection.execute(
                    "UPDATE mail_outbox SET status='sending',attempts=attempts+1,updated_at=? "
                    "WHERE brief_id=?",
                    (timestamp(at), row["brief_id"]),
                )
                connection.execute(
                    "UPDATE mail_dispatch SET token=?,expires_at=?,next_at=NULL,error_code=NULL,"
                    "cycle_attempts=cycle_attempts+1 WHERE brief_id=?",
                    (token, timestamp(expiry), row["brief_id"]),
                )
                connection.execute(
                    "INSERT INTO mail_attempts(brief_id,number,token,started_at,status) "
                    "VALUES(?,?,?,?,'sending')",
                    (row["brief_id"], row["attempts"] + 1, token, timestamp(at)),
                )
                self._event(
                    connection,
                    row,
                    "mail_sending",
                    {"status": "sending", "attempt": row["attempts"] + 1},
                )
                return record, meta["message_id"], token
        return None

    def admit(self, brief_id, token):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT m.*,d.token,d.expires_at FROM mail_outbox m JOIN mail_dispatch d "
                "ON d.brief_id=m.brief_id WHERE m.brief_id=?",
                (brief_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "sending"
                or row["token"] != token
                or row["expires_at"] <= timestamp(self.runs.now())
            ):
                return False
            run = _record(
                connection.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            )
            return (
                run.subscription.enabled and self.outbox.delivery_enabled(connection, run) is True
            )

    def finish(self, brief_id, token, result):
        if not isinstance(result, SendOutcome) or result.status not in {
            "sent",
            "retry",
            "failed",
            "unknown",
        }:
            result = SendOutcome("unknown", "SMTP_RESULT_UNKNOWN")
        at = self.runs.now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mail_outbox WHERE brief_id=?", (brief_id,)
            ).fetchone()
            meta = self._meta(connection, row)
            if row["status"] != "sending" or meta["token"] != token:
                raise LeaseLostError("mail send capability lost")
            if meta["expires_at"] <= timestamp(at):
                self._unknown(connection, row)
                return "unknown"
            retry = (
                result.status == "retry"
                and meta["cycle_attempts"] < self.settings.mail_max_attempts
            )
            status = "pending" if retry else "failed" if result.status == "retry" else result.status
            next_at = (
                timestamp(at + timedelta(seconds=self.settings.mail_retry_seconds))
                if retry
                else None
            )
            code = result.code if result.status != "retry" or retry else "MAIL_RETRY_EXHAUSTED"
            connection.execute(
                "UPDATE mail_outbox SET status=?,updated_at=? WHERE brief_id=?",
                (status, timestamp(at), brief_id),
            )
            connection.execute(
                "UPDATE mail_dispatch SET token=NULL,expires_at=NULL,next_at=?,error_code=? "
                "WHERE brief_id=?",
                (next_at, code, brief_id),
            )
            connection.execute(
                "UPDATE mail_attempts SET status=?,finished_at=?,error_code=? WHERE token=?",
                (result.status, timestamp(at), code, token),
            )
            self._event(
                connection,
                row,
                "mail_retry_waiting" if retry else "mail_" + status,
                {
                    "status": status,
                    "error_code": code,
                    "retry_at": next_at,
                    "attempt": row["attempts"],
                },
            )
            return status

    def status(self, brief_id, owner):
        self.briefs.get(brief_id, owner)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM mail_outbox WHERE brief_id=?", (brief_id,)
            ).fetchone()
            if row is None:
                return None
            record = self.outbox._record(row)
            meta = connection.execute(
                "SELECT * FROM mail_dispatch WHERE brief_id=?", (brief_id,)
            ).fetchone()
        return {
            "brief_id": brief_id,
            "status": record.status,
            "attempts": record.attempts,
            "updated_at": record.updated_at.isoformat(),
            "retry_at": meta["next_at"] if meta else None,
            "error_code": meta["error_code"] if meta else None,
            "can_resend": record.status in {"failed", "unknown"},
            "confirmation_required": record.status == "unknown",
        }

    def resend(self, brief_id, owner, *, confirm_duplicate=False):
        self.briefs.get(brief_id, owner)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mail_outbox WHERE brief_id=?", (brief_id,)
            ).fetchone()
            if row is None or row["status"] not in {"failed", "unknown"}:
                raise ProductError("MAIL_NOT_RETRYABLE")
            self.outbox._record(row)
            if row["status"] == "unknown" and not confirm_duplicate:
                raise ProductError("CONFIRM_DUPLICATE_DELIVERY")
            run = _record(self.runs._owned_row(connection, row["run_id"], owner))
            if (
                not run.subscription.enabled
                or self.outbox.delivery_enabled(connection, run) is not True
            ):
                raise ProductError("SUBSCRIPTION_DISABLED")
            self._meta(connection, row)
            connection.execute(
                "UPDATE mail_outbox SET status='pending',updated_at=? WHERE brief_id=?",
                (timestamp(self.runs.now()), brief_id),
            )
            connection.execute(
                "UPDATE mail_dispatch SET token=NULL,expires_at=NULL,next_at=NULL,error_code=NULL,"
                "cycle_attempts=0 WHERE brief_id=?",
                (brief_id,),
            )
            self._event(
                connection,
                row,
                "mail_resend_requested",
                {
                    "status": "pending",
                    "reason": "explicit_user_confirmation"
                    if confirm_duplicate
                    else "explicit_user_retry",
                },
            )
        return self.status(brief_id, owner)
