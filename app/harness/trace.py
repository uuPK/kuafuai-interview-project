"""Bounded event projections for task details and committed JSON logs."""

import json
import logging
import math
import re

from app.harness.redaction import Redactor

LOG = logging.getLogger("app.events")

# Full protocol, drafts, bodies, recipients and execution capabilities stay in trusted stores.
FIELDS = set(
    "tool_call_id request_id name kind mode from to stop_reason retry_at attempt attempts "
    "max_attempts category retryable error_code ok turn tool_calls arguments_chars result_chars "
    "parameters query language domains limit path offset keyword dir command candidate_id feed_id "
    "content_chars source_id brief_id candidate_ids received_count omitted_count filtered provider "
    "provider_request_id response_id content_hash char_count publication_status fetched_at "
    "item_count event_count review_basis approved issue_count response_valid usage prompt_tokens "
    "completion_tokens total_tokens charge model_requests searches fetches timeout_seconds "
    "collected missing issues field issue basis items title topics selection_reason "
    "submission_attempts revisions_remaining worker_id container_count file_count status "
    "reason elapsed_ms file_path start_offset end_offset next_offset eof truncated "
    "match_count entry_count history_count parameters_omitted result_summary days".split()
)
LABELS = {
    "budget_warning": "研究预算接近上限，提示模型预留提交与修订空间",
    "run_created": "任务已创建",
    "state_changed": "任务状态变化",
    "model_response_saved": "模型响应已保存",
    "request_attempt_started": "开始请求",
    "request_attempt_failed": "请求失败",
    "tool_pending": "准备调用工具",
    "tool_executing": "开始执行工具",
    "tool_completed": "工具执行结束",
    "tool_feedback_saved": "结果已反馈给模型",
    "search_discovered": "候选发现结果已保存",
    "source_saved": "固定正文已保存",
    "progress_updated": "模型更新研究进度",
    "research_selections": "模型记录选题理由",
    "brief_review_completed": "独立核对完成",
    "brief_check_failed": "稿件检查未通过",
    "brief_saved": "简报已保存",
    "mail_queued": "邮件已进入待发送队列",
    "mail_suppressed": "本次未入邮件队列",
    "worker_claimed": "Worker 领取任务",
    "worker_reclaimed": "Worker 接管任务",
    "tool_call_recovered": "中断工具调用已处理",
    "model_request_recovered": "中断模型请求已处理",
    "recovery_failed": "任务恢复失败",
    "bash_recovery_cleaned": "残留工具容器已清理",
    "mail_sending": "开始发送邮件",
    "mail_sent": "邮件服务器已接受",
    "mail_failed": "邮件发送未完成",
    "mail_unknown": "邮件发送结果不确定",
    "mail_retry_waiting": "邮件等待有限重试",
    "mail_resend_requested": "用户请求重新发送",
}


def configure_event_logging():
    if not LOG.hasHandlers():
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


class EventProjection:
    def __init__(self, settings):
        self.redactor = Redactor(settings)

    def text(self, value, maximum=500):
        value = self.redactor.clean(value)
        value = re.sub(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", "[EMAIL]", value)
        value = re.sub(
            r"(?i)\b(password|secret|api[-_]?key|(?:access[-_]?)?token)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            value,
        )
        return value if len(value) <= maximum else value[:maximum] + "…"

    def _tree(self, value, depth=0):
        if depth > 5:
            return "[OMITTED]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                key: self._tree(item, depth + 1) for key, item in value.items() if key in FIELDS
            }
        if isinstance(value, (list, tuple)):
            return [self._tree(item, depth + 1) for item in value[:20]]
        if type(value) is float and not math.isfinite(value):
            return "[OMITTED]"
        return value if value is None or type(value) in {bool, int, float} else "[OMITTED]"

    def data(self, value):
        result = self._tree(value)
        if not isinstance(result, dict):
            return {}
        if len(json.dumps(result, ensure_ascii=False)) > 12_000:
            return {"parameters_omitted": True}
        return result

    def emit(self, row):
        record = {
            "event_id": row["id"],
            "run_id": self.text(row["run_id"], 128),
            "event": self.text(row["kind"], 100),
            "occurred_at": row["occurred_at"],
            "data": self.data(json.loads(row["data_json"])),
        }
        LOG.info(json.dumps(record, ensure_ascii=False, allow_nan=False))


def argument_summary(name, raw):
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return {"parameters_omitted": True}
    if not isinstance(value, dict):
        return {"parameters_omitted": True}
    allowed = {
        "web_search": {"query", "language", "domains", "limit"},
        "read_feed": {"feed_id", "limit"},
        "fetch_url": {"candidate_id"},
        "list_dir": {"path"},
        "read_file": {"path", "offset", "limit"},
        "search_content": {"keyword", "dir", "limit"},
        "write_file": {"path"},
        "bash": {"command"},
        "query_history": {"days", "limit"},
        "update_progress": {"collected", "missing", "issues"},
    }.get(name, set())
    summary = {key: item for key, item in value.items() if key in allowed}
    if name == "write_file" and isinstance(value.get("content"), str):
        summary["content_chars"] = len(value["content"])
    if name == "submit_brief" and isinstance(value.get("draft"), dict):
        items = value["draft"].get("items")
        if isinstance(items, list):
            summary["item_count"] = len(items)
    return summary


def result_summary(result):
    if not result.ok:
        return {"issues": [issue.model_dump() for issue in result.error.details]}
    if not isinstance(result.data, dict):
        return {}
    return {
        key: value
        for key, value in result.data.items()
        if key
        in {
            "source_id",
            "brief_id",
            "received_count",
            "omitted_count",
            "file_path",
            "char_count",
            "start_offset",
            "end_offset",
            "next_offset",
            "eof",
            "truncated",
            "match_count",
            "entry_count",
            "history_count",
        }
    }
