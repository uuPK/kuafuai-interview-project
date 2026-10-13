"""Formal submission rules with a trusted, mandatory independent-review seam."""

from app.agent.briefs import ReviewOutcome, SubmitParameters
from app.tools.models import ToolFailure
from app.tools.registry import ToolDefinition


class SubmissionTool:
    def __init__(self, store, *, reviewer=None, review_basis="independent"):
        if review_basis not in {"independent", "fixture"}:
            raise ValueError("invalid trusted review basis")
        self.store, self.reviewer, self.review_basis = store, reviewer, review_basis

    async def submit(self, context, parameters):
        if context.tool_call_id is None:
            raise PermissionError("submission requires a server-bound call")
        prepared = self.store.prepared(context, parameters.draft)
        if prepared is not None:
            return prepared
        checked, rejected = self.store.rules.check(context, parameters.draft)
        if rejected is not None:
            return rejected
        if self.reviewer is None:
            raise ToolFailure(
                "BRIEF_REVIEW_UNAVAILABLE",
                "提交规则通过，独立质量核对尚未接入，无法完成正式交付。",
                category="configuration",
            )
        outcome = ReviewOutcome.model_validate(
            await self.reviewer(context, parameters.draft, checked)
        )
        if not outcome.approved:
            return self.store.rules.rejection(outcome.issues)
        return self.store.stage(context, parameters.draft, checked, self.review_basis)


def submission_completion(result):
    if result.ok and isinstance(result.data, dict) and result.data.get("accepted") is True:
        return result.data["status"]
    return None


def register_submission(registry, store, *, reviewer=None, review_basis="independent"):
    tool = SubmissionTool(store, reviewer=reviewer, review_basis=review_basis)
    registry.register(
        ToolDefinition(
            "submit_brief",
            "检查并提交结构化中文简报draft。items最多5条且不超过订阅上限；"
            "每条带title/summary/topics/event_id/selection_reason/key_facts/citations，引用带"
            "source_id/start_offset/end_offset/quote；quote必须逐字符匹配固定正文。"
            "每个话题须有入选条目或uncovered_topics中的topic/reason；条数不足时提供shortfall_reason。"
            "空简报须先有有效搜索/RSS结果，依赖或材料失败不能当作无新闻。"
            "服务端检查归属、时间、哈希和排除词并生成链接；独立核对通过才保存。",
            SubmitParameters,
            tool.submit,
            completion_status=submission_completion,
            on_completed=store.on_completed,
        )
    )
