"""Bind an independent review input/response to one durable submission call."""

import json

from app.agent.protocol import ModelRequest, ModelResponse
from app.harness.redaction import Redactor
from app.storage.models import BudgetCharge
from app.storage.runs import _record
from app.storage.snapshots import digest
from app.tools.models import ToolFailure, ToolInvocation


class ReviewStore:
    def __init__(self, calls, settings):
        self.calls, self.runs, self.database = calls, calls.runs, calls.database
        self.redactor = Redactor(settings)

    @staticmethod
    def _problem():
        raise ToolFailure("BRIEF_REVIEW_STATE_PROBLEM", "已保存的核对输入、响应或调用关系不一致。")

    def _call(self, connection, context):
        run = _record(self.runs._owned_row(connection, context.run_id, context.user_id))
        call = self.calls._row(connection, context.run_id, context.tool_call_id)
        fingerprint = ToolInvocation(
            tool_call_id=call["tool_call_id"], name=call["name"], arguments=call["arguments"]
        ).fingerprint
        if (
            run.status != "running"
            or call["name"] != "submit_brief"
            or call["status"] != "executing"
            or call["invocation_hash"] != fingerprint
        ):
            self._problem()
        return run, call

    def _validate(self, row, call, request):
        try:
            saved = ModelRequest.model_validate_json(row["request_json"])
            if (
                row["invocation_hash"] != call["invocation_hash"]
                or row["input_hash"] != digest(row["request_json"].encode("utf-8"))
                or saved != request
            ):
                self._problem()
            return (
                ModelResponse.model_validate_json(row["response_json"])
                if row["response_json"] is not None
                else None
            )
        except (ValueError, TypeError):
            self._problem()

    def prepare(self, context, request):
        encoded = request.model_dump_json()
        with self.database.transaction() as connection:
            _, call = self._call(connection, context)
            row = connection.execute(
                "SELECT * FROM brief_reviews WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
            if row is not None:
                return self._validate(row, call, request)
            connection.execute(
                "INSERT INTO brief_reviews VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    context.run_id,
                    context.tool_call_id,
                    call["invocation_hash"],
                    digest(encoded.encode("utf-8")),
                    encoded,
                ),
            )
        return None

    def start_attempt(self, context):
        with self.database.transaction() as connection:
            run, _ = self._call(connection, context)
            request = connection.execute(
                "SELECT * FROM external_requests WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
            if request is None or request["status"] != "executing":
                self._problem()
            at = self.runs.now()
            self.runs._consume_budget(connection, run, BudgetCharge(model_requests=1), at)
            self.runs._append_event(
                connection,
                run.id,
                "brief_review_attempt_started",
                at,
                {
                    "tool_call_id": context.tool_call_id,
                    "request_id": request["id"],
                    "attempt": request["attempts"],
                },
            )

    def save_response(self, context, request, response, outcome):
        response = ModelResponse.model_validate_json(
            json.dumps(self.redactor.clean(response.model_dump(mode="json")), ensure_ascii=False)
        )
        with self.database.transaction() as connection:
            _, call = self._call(connection, context)
            row = connection.execute(
                "SELECT * FROM brief_reviews WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
            if row is None:
                self._problem()
            saved = self._validate(row, call, request)
            if saved is not None:
                if saved != response:
                    self._problem()
                return
            connection.execute(
                "UPDATE brief_reviews SET response_json=? WHERE run_id=? AND tool_call_id=?",
                (response.model_dump_json(), context.run_id, context.tool_call_id),
            )
            self.runs._append_event(
                connection,
                context.run_id,
                "brief_review_completed",
                self.runs.now(),
                {
                    "tool_call_id": context.tool_call_id,
                    "approved": outcome.approved if outcome is not None else None,
                    "issue_count": len(outcome.issues) if outcome is not None else None,
                    "response_valid": outcome is not None,
                    "usage": {
                        key: value
                        for key, value in response.usage.items()
                        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and type(value) is int
                        and value >= 0
                    },
                    "response_id": response.response_id,
                },
            )

    def service_failure(self, context, code):
        with self.database.transaction() as connection:
            run, _ = self._call(connection, context)
            self.runs._append_event(
                connection,
                run.id,
                "brief_review_service_failed",
                self.runs.now(),
                {"tool_call_id": context.tool_call_id, "error_code": code},
            )
