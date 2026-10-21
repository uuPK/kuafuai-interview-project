"""Reserve one immutable recipient/message per accepted scheduled brief; no SMTP here."""

import json
import re
from typing import Literal

from pydantic import AwareDatetime, Field

from app.agent.briefs import BriefDocument
from app.storage.models import StoredModel, timestamp
from app.storage.runs import _record
from app.storage.snapshots import digest
from app.tools.models import ToolFailure


class MailRecord(StoredModel):
    brief_id: str
    run_id: str
    recipient: str = Field(repr=False)
    subject: str = Field(repr=False)
    body_text: str = Field(repr=False)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["pending", "sending", "sent", "failed", "unknown"]
    attempts: int = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime


def message_hash(recipient, subject, body):
    return digest(json.dumps([recipient, subject, body], ensure_ascii=False).encode("utf-8"))


def render_message(run, document, *, recipient=None):
    recipient = recipient if recipient is not None else run.subscription.email
    if (
        len(recipient) > 254
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", recipient)
        or recipient.split("@")[0].startswith(".")
        or recipient.split("@")[0].endswith(".")
        or ".." in recipient
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
            for part in recipient.split("@")[1].split(".")
        )
    ):
        raise ToolFailure("MAIL_RECIPIENT_INVALID", "服务端订阅邮箱格式无效，未完成交付。")
    subject = "每日 AI 新闻简报｜" + " ".join(document.title.split())
    lines = [
        document.title,
        f"新闻窗口：{timestamp(run.window_start)} 至 {timestamp(run.window_end)}",
        "",
    ]
    if document.review_status == "unverified":
        lines.append("最终事实核对仍有问题，本次仅交付待核实原文线索，未确认任何主新闻。")
    elif document.review_status == "partial":
        lines.append("已保留通过原文核对的新闻；仍有疑点的条目单列在待核实信息中。")
    if not document.items:
        lines.append(
            "本次尚无可确认的主新闻，以下候选仍待核实。"
            if document.unverified_items
            else "暂无符合订阅条件的新闻。"
        )
    for number, item in enumerate(document.items, 1):
        lines.extend(
            [
                f"{number}. {item.title}",
                *("信息说明：" + note for note in item.verification_notes),
                item.summary,
                f"入选理由：{item.selection_reason}",
            ]
        )
        lines.extend(f"来源：{url}" for url in dict.fromkeys(c.url for c in item.citations))
        lines.append("")
    if document.unverified_items:
        lines.extend(["待核实信息（尚未确认，不计入主新闻条数）", ""])
        for item in document.unverified_items:
            lines.append(item.title)
            if item.summary:
                lines.extend(["简介尚未核实：" + item.summary])
            if item.topics:
                lines.append("相关话题：" + "、".join(item.topics))
            lines.extend(["待核实原因：" + item.reason, "原文：" + item.url, ""])
    lines.extend(f"未覆盖话题：{item.topic}；{item.reason}" for item in document.uncovered_topics)
    if document.shortfall_reason:
        lines.append("条数说明：" + document.shortfall_reason)
    return recipient, subject, "\n".join(lines)


class OutboxStore:
    def __init__(self, runs, *, delivery_enabled=None):
        self.runs, self.database = runs, runs.database
        # Stage 4's live subscription store can supply a check using this same transaction.
        self.delivery_enabled = delivery_enabled or (
            lambda connection, run: run.subscription.enabled
        )

    @staticmethod
    def _problem():
        raise ToolFailure("MAIL_STATE_PROBLEM", "发送记录或固定邮件内容不一致。")

    def reserve(
        self, connection, run_id, user_id, brief_id, *, manual=False, recipient=None, document=None
    ):
        if not connection.in_transaction:
            raise ValueError("mail reservation requires the brief transaction")
        run = _record(self.runs._owned_row(connection, run_id, user_id))
        row = connection.execute(
            "SELECT * FROM briefs WHERE id=? AND run_id=?", (brief_id, run_id)
        ).fetchone()
        if row is None or run.status not in {"completed", "no_news"}:
            raise PermissionError("mail requires an accepted saved brief")
        if run.mode != "scheduled" and not manual:
            return None
        existing = connection.execute(
            "SELECT * FROM mail_outbox WHERE brief_id=?", (brief_id,)
        ).fetchone()
        if manual and existing is not None:
            saved = self._record(existing)
            if saved.run_id != run_id:
                self._problem()
            return saved
        enabled = self.delivery_enabled(connection, run)
        if type(enabled) is not bool:
            raise TypeError("delivery eligibility must return a boolean synchronously")
        if not run.subscription.enabled or not enabled:
            self.runs._append_event(
                connection,
                run_id,
                "mail_suppressed",
                self.runs.now(),
                {"brief_id": brief_id, "reason": "subscription_disabled"},
            )
            return None
        try:
            document = (
                document
                if manual and document is not None
                else BriefDocument.model_validate_json(row["document_json"])
            )
        except ValueError:
            self._problem()
        recipient, subject, body = render_message(
            run, document, recipient=recipient if manual else None
        )
        payload_hash = message_hash(recipient, subject, body)
        if existing is not None:
            saved = self._record(existing)
            if saved.run_id != run_id or saved.payload_hash != payload_hash:
                self._problem()
            return saved
        at = timestamp(self.runs.now())
        connection.execute(
            "INSERT INTO mail_outbox VALUES(?,?,?,?,?,?,'pending',0,?,?)",
            (brief_id, run_id, recipient, subject, body, payload_hash, at, at),
        )
        self.runs._append_event(
            connection,
            run_id,
            "mail_queued",
            self.runs.now(),
            {"brief_id": brief_id, "status": "pending"},
        )
        return self._record(
            connection.execute("SELECT * FROM mail_outbox WHERE brief_id=?", (brief_id,)).fetchone()
        )

    def _record(self, row):
        try:
            record = MailRecord.model_validate(dict(row))
            if (
                message_hash(record.recipient, record.subject, record.body_text)
                != record.payload_hash
            ):
                self._problem()
            return record
        except ValueError:
            self._problem()

    def get(self, brief_id, user_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT m.* FROM mail_outbox m "
                "JOIN briefs b ON b.id=m.brief_id AND b.run_id=m.run_id "
                "JOIN runs r ON r.id=m.run_id WHERE m.brief_id=? AND r.user_id=?",
                (brief_id, user_id),
            ).fetchone()
            if row is None:
                raise LookupError("mail record not found for this user")
            self.runs._owned_row(connection, row["run_id"], user_id)
            return self._record(row)
