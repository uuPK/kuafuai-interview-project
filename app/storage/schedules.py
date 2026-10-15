"""Shanghai daily scheduling with durable cursors, daily uniqueness and missed ranges."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.auth.models import AuthError
from app.storage.models import RunRequest, SubscriptionSnapshot, timestamp
from app.tools.models import ToolFailure

SHANGHAI = ZoneInfo("Asia/Shanghai")


def slot(day, push_time):
    return datetime.combine(day, time.fromisoformat(push_time), SHANGHAI).astimezone(UTC)


def next_slot(at, push_time):
    day = at.astimezone(SHANGHAI).date()
    candidate = slot(day, push_time)
    return candidate if candidate >= at else slot(day + timedelta(days=1), push_time)


class ScheduleStore:
    def __init__(self, runs, subscriptions):
        self.runs, self.subscriptions, self.database = runs, subscriptions, runs.database

    def changed(self, connection, before, after, at):
        previous = connection.execute(
            "SELECT * FROM subscription_schedules WHERE subscription_id=?", (after["id"],)
        ).fetchone()
        reset = (
            previous is None
            or before is None
            or any(before[key] != after[key] for key in ("enabled", "push_time"))
            or previous["error_code"] is not None
        )
        if reset:
            due = timestamp(next_slot(at, after["push_time"])) if after["enabled"] else None
            connection.execute(
                "INSERT INTO subscription_schedules VALUES(?,?,NULL) "
                "ON CONFLICT(subscription_id) DO UPDATE SET "
                "next_at=excluded.next_at,error_code=NULL",
                (after["id"], due),
            )

    @staticmethod
    def summary(connection, subscription_id):
        state = connection.execute(
            "SELECT next_at,error_code FROM subscription_schedules WHERE subscription_id=?",
            (subscription_id,),
        ).fetchone()
        notice = connection.execute(
            "SELECT first_at,last_at,missed_count,reason FROM schedule_notices "
            "WHERE subscription_id=? ORDER BY id DESC LIMIT 1",
            (subscription_id,),
        ).fetchone()
        return {
            "timezone": "Asia/Shanghai",
            "next_at": state["next_at"] if state else None,
            "error_code": state["error_code"] if state else None,
            "last_notice": dict(notice) if notice else None,
        }

    def _notice(self, connection, identifier, first, last, count, at):
        connection.execute(
            "INSERT OR IGNORE INTO schedule_notices "
            "(subscription_id,first_at,last_at,missed_count,reason,created_at) VALUES(?,?,?,?,?,?)",
            (
                identifier,
                timestamp(first),
                timestamp(last),
                count,
                "outside_catchup_window",
                timestamp(at),
            ),
        )

    def _one(self, identifier, at):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                return 0
            saved = self.subscriptions._record(row)
            state = connection.execute(
                "SELECT * FROM subscription_schedules WHERE subscription_id=?", (identifier,)
            ).fetchone()
            if state is None:
                self.changed(connection, None, saved, datetime.fromisoformat(row["created_at"]))
                state = connection.execute(
                    "SELECT * FROM subscription_schedules WHERE subscription_id=?", (identifier,)
                ).fetchone()
            if not saved["enabled"] or state["next_at"] is None:
                return 0
            due = datetime.fromisoformat(state["next_at"])
            if due > at:
                return 0
            day = at.astimezone(SHANGHAI).date()
            latest = slot(day, saved["push_time"])
            if latest > at:
                day -= timedelta(days=1)
                latest = slot(day, saved["push_time"])
            older = (day - due.astimezone(SHANGHAI).date()).days
            if older > 0:
                self._notice(
                    connection,
                    identifier,
                    due,
                    slot(day - timedelta(days=1), saved["push_time"]),
                    older,
                    at,
                )
            created = 0
            if at - latest > timedelta(hours=2):
                self._notice(connection, identifier, latest, latest, 1, at)
            elif (
                connection.execute(
                    "SELECT 1 FROM scheduled_runs WHERE user_id=? AND subscription_id=? "
                    "AND business_date=?",
                    (row["user_id"], identifier, day.isoformat()),
                ).fetchone()
                is None
            ):
                account = self.subscriptions._account(connection, row["user_id"])
                snapshot = SubscriptionSnapshot(
                    nickname=account.nickname,
                    email=account.email,
                    **{
                        key: saved[key]
                        for key in (
                            "topics",
                            "keywords",
                            "exclude_keywords",
                            "max_items",
                            "enabled",
                            "push_time",
                        )
                    },
                )
                self.subscriptions._safe(snapshot)
                run = self.runs._create_run(
                    connection,
                    RunRequest(
                        user_id=account.id,
                        subscription_id=identifier,
                        subscription=snapshot,
                        mode="scheduled",
                        scheduled_for=latest,
                    ),
                    at,
                )
                connection.execute(
                    "INSERT INTO scheduled_runs VALUES(?,?,?,?)",
                    (
                        account.id,
                        identifier,
                        day.isoformat(),
                        run.id,
                    ),
                )
                created = 1
            connection.execute(
                "UPDATE subscription_schedules SET next_at=? WHERE subscription_id=?",
                (timestamp(slot(day + timedelta(days=1), saved["push_time"])), identifier),
            )
            return created

    def tick(self):
        at = self.runs.now()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT s.id FROM subscriptions s LEFT JOIN subscription_schedules t "
                "ON t.subscription_id=s.id WHERE t.subscription_id IS NULL OR t.next_at<=? "
                "ORDER BY t.next_at,s.id LIMIT 100",
                (timestamp(at),),
            ).fetchall()
        created = 0
        for row in rows:
            try:
                created += self._one(row["id"], at)
            except (AuthError, ToolFailure, ValueError, TypeError):
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO subscription_schedules "
                        "VALUES(?,NULL,'SCHEDULE_STATE_PROBLEM') "
                        "ON CONFLICT(subscription_id) DO UPDATE SET "
                        "next_at=NULL,error_code=excluded.error_code",
                        (row["id"],),
                    )
        return created
