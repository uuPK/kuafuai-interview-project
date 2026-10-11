"""Derive repeat failures from durable feedback and reserve a decision/submission slot."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from app.storage.models import RunRecord
from app.storage.runs import RunStore, _record
from app.tools.models import ToolInvocation, ToolResult

IGNORED_ERRORS = {
    "RUN_FINISHED",
    "BUDGET_EXCEEDED",
    "SUBMISSION_REQUIRED",
    "ACTION_CHANGE_REQUIRED",
    "REPEATED_ACTION_STOPPED",
}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def action_hash(name: str, arguments: str) -> str:
    try:
        arguments_value = json.loads(arguments, object_pairs_hook=_unique_object)
        encoded = json.dumps(
            [name, arguments_value], sort_keys=True, ensure_ascii=True, allow_nan=False
        )
    except (ValueError, RecursionError):
        encoded = json.dumps([name, arguments], ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class FailureStreak:
    action: str
    error_code: str
    count: int
    last_call_id: str


class GuardrailStore:
    def __init__(self, runs: RunStore) -> None:
        self.runs, self.database = runs, runs.database

    @staticmethod
    def streak(connection: sqlite3.Connection, run_id: str) -> FailureStreak | None:
        rows = connection.execute(
            """SELECT c.* FROM run_messages f JOIN tool_calls c USING (run_id, tool_call_id)
                WHERE f.run_id = ? AND f.role = 'tool' ORDER BY f.seq DESC""",
            (run_id,),
        )
        first, count = None, 0
        for row in rows:
            result = ToolResult.model_validate_json(row["result_json"])
            if result.error and result.error.code in IGNORED_ERRORS:
                continue
            if not result.ok:
                code = result.error.code
            elif (
                row["name"] == "web_search"
                and isinstance(result.data, dict)
                and result.data.get("items") == []
            ):
                code = "NO_RESULTS"
            else:
                break
            action = action_hash(row["name"], row["arguments"])
            if first is None:
                first = (action, code, row["tool_call_id"])
            elif first[0] != action:
                break
            count += 1
        return FailureStreak(first[0], first[1], count, first[2]) if first else None

    @staticmethod
    def has_notice(connection, run_id: str, key: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM harness_notices WHERE run_id = ? AND notice_key = ?", (run_id, key)
            ).fetchone()
            is not None
        )

    def before_new_call(
        self,
        run_id: str,
        user_id: str,
        invocation: ToolInvocation,
        *,
        is_submission: bool,
    ) -> ToolResult | None:
        with self.database.connection() as connection:
            run = _record(self.runs._owned_row(connection, run_id, user_id))
            if run.status != "running":
                return None
            if (
                self.runs.now() >= run.deadline_at
                or run.budget_usage.tool_calls >= run.budget_limits.tool_calls
            ):
                return ToolResult.failure("BUDGET_EXCEEDED", "任务预算或截止时间已耗尽。")
            if not is_submission and (
                run.budget_limits.tool_calls - run.budget_usage.tool_calls <= 1
                or run.budget_limits.model_requests - run.budget_usage.model_requests <= 0
            ):
                return ToolResult.failure(
                    "SUBMISSION_REQUIRED", "剩余预算已预留给提交，请调用提交工具。"
                )
            streak = self.streak(connection, run_id)
            if (
                streak
                and streak.count >= 3
                and streak.action == action_hash(invocation.name, invocation.arguments)
            ):
                warned = self.has_notice(connection, run_id, f"repeat:{streak.last_call_id}")
                return ToolResult.failure(
                    "REPEATED_ACTION_STOPPED" if warned else "ACTION_CHANGE_REQUIRED",
                    "同一动作已连续三次失败，请根据反馈改变行动。",
                )
            return None

    def close_to_limit(self, run: RunRecord, request_timeout: float) -> bool:
        return (
            run.budget_limits.model_requests - run.budget_usage.model_requests <= 2
            or run.budget_limits.tool_calls - run.budget_usage.tool_calls <= 2
            or run.budget_limits.searches - run.budget_usage.searches <= 1
            or run.budget_limits.fetches - run.budget_usage.fetches <= 1
            or (run.deadline_at - self.runs.now()).total_seconds() <= 2 * request_timeout
        )
