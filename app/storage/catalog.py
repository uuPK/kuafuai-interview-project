"""Owner-checked, bounded public views without raw protocol or material paths."""

from app.product.models import ProductError
from app.product.paging import Cursors
from app.storage.models import RunStatus
from app.storage.runs import _record
from app.tools.models import ToolFailure

RUN_FIELDS = (
    "id",
    "mode",
    "status",
    "created_at",
    "updated_at",
    "scheduled_for",
    "window_start",
    "window_end",
    "started_at",
    "deadline_at",
    "retry_at",
    "stop_reason",
    "budget_limits",
    "budget_usage",
)

SOURCE_ERRORS = {
    "PUBLIC_ADDRESS_BLOCKED",
    "PUBLIC_PEER_MISMATCH",
    "PUBLIC_DNS_INVALID",
    "PUBLIC_DNS_TIMEOUT",
    "PUBLIC_DNS_UNAVAILABLE",
    "FETCH_NETWORK",
    "FETCH_TIMEOUT",
    "FETCH_FORBIDDEN",
    "FETCH_HTTP_STATUS",
    "FETCH_TOO_LARGE",
}


class CatalogStore:
    def __init__(self, research, briefs, settings):
        self.research, self.briefs = research, briefs
        self.runs, self.database = research.runs, research.database
        self.cursors = Cursors(settings)

    def _page(self, owner, kind, cursor, limit):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ProductError("INVALID_PAGE", 422)
        position = self.cursors.decode(owner, kind, cursor)
        if kind == "runs":
            query = "SELECT r.* FROM runs r WHERE r.user_id=?"
        else:
            query = (
                "SELECT b.id,b.created_at FROM briefs b JOIN runs r ON r.id=b.run_id "
                "WHERE r.user_id=? "
                "AND r.status IN ('completed','no_news')"
            )
        alias = "r" if kind == "runs" else "b"
        params = [owner]
        if position:
            query += f" AND ({alias}.created_at,{alias}.id)<(?,?)"
            params.extend(position)
        query += f" ORDER BY {alias}.created_at DESC,{alias}.id DESC LIMIT ?"
        params.append(limit + 1)
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        visible = rows[:limit]
        return visible, (
            self.cursors.encode(owner, kind, visible[-1]["created_at"], visible[-1]["id"])
            if len(rows) > limit
            else None
        )

    def _run(self, row):
        try:
            run = _record(row)
        except (ValueError, TypeError):
            raise ToolFailure("RUN_STATE_PROBLEM", "任务记录损坏，无法展示。") from None
        value = run.model_dump(mode="json", include=set(RUN_FIELDS))
        return self.runs.projection.redactor.clean(value)

    def runs_page(self, owner, *, cursor=None, limit=20):
        rows, next_cursor = self._page(owner, "runs", cursor, limit)
        return {"items": [self._run(row) for row in rows], "next_cursor": next_cursor}

    def run(self, run_id, owner):
        with self.database.connection() as connection:
            row = self.runs._owned_row(connection, run_id, owner)
            brief = connection.execute("SELECT id FROM briefs WHERE run_id=?", (run_id,)).fetchone()
            source_error = self._source_error(connection, row)
        value = self._run(row)
        run = _record(row)
        value["subscription"] = self.runs.projection.redactor.clean(
            run.subscription.model_dump(mode="json")
        )
        value["brief_id"] = brief["id"] if brief else None
        value["source_error_code"] = source_error
        value["progress"] = self.runs.projection.data(
            self.research.progress(run_id, owner).model_dump()
        )
        return value

    @staticmethod
    def _source_error(connection, run):
        """Explain evidence acquisition failure using saved results, never model prose."""
        if run["status"] != "failed":
            return None
        rows = connection.execute(
            "SELECT json_extract(result_json,'$.ok') AS ok, "
            "json_extract(result_json,'$.error.code') AS code FROM tool_calls "
            "WHERE run_id=? AND name IN ('fetch_url','read_feed') "
            "AND json_valid(result_json) ORDER BY rowid DESC LIMIT 100",
            (run["id"],),
        ).fetchall()
        if connection.execute(
            "SELECT 1 FROM sources WHERE run_id=? LIMIT 1", (run["id"],)
        ).fetchone():
            return None
        return next(
            (row["code"] for row in rows if not row["ok"] and row["code"] in SOURCE_ERRORS), None
        )

    def brief(self, brief_id, owner):
        value = self.briefs.get(brief_id, owner)
        try:
            run = self.runs.get_run(value["run_id"], owner)
        except ValueError:
            raise ToolFailure("RUN_STATE_PROBLEM", "任务记录损坏，无法展示。") from None
        if run.status not in {RunStatus.COMPLETED, RunStatus.NO_NEWS}:
            raise ToolFailure("BRIEF_STATE_PROBLEM", "简报与任务状态不一致，无法展示。")
        value["document"] = value["document"].model_dump(mode="json")
        value["status"], value["mode"] = run.status, run.mode
        return self.runs.projection.redactor.clean(value)

    def briefs_page(self, owner, *, cursor=None, limit=20):
        rows, next_cursor = self._page(owner, "briefs", cursor, limit)
        items = []
        for row in rows:
            value = self.brief(row["id"], owner)
            document = value.pop("document")
            value["title"], value["item_count"] = document["title"], len(document["items"])
            items.append(value)
        return {"items": items, "next_cursor": next_cursor}
