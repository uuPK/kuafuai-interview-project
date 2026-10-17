"""Read delivered facts and persist bounded research progress, never publish news."""

from pydantic import Field

from app.agent.research import ResearchProgress
from app.tools.models import ArgumentIssue, ToolFailure, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition


class HistoryParameters(ToolParameters):
    days: int = Field(default=7, ge=1, le=30)
    limit: int = Field(default=10, ge=1, le=20)


class ResearchTools:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    async def update_progress(self, context, parameters):
        if context.tool_call_id is None:
            raise PermissionError("progress requires a server-bound call")
        run = self.store.runs.get_run(context.run_id, context.user_id)
        topics = {topic.casefold(): topic for topic in run.subscription.topics}
        if any(
            topic.casefold() not in topics for topic in (*parameters.collected, *parameters.missing)
        ):
            return ToolResult.failure(
                "PROGRESS_TOPIC_UNKNOWN",
                "collected/missing只能填写订阅原话题，事件说明放issues。",
                details=tuple(
                    ArgumentIssue(field="collected/missing", issue=f"允许话题：{topic}"[:80])
                    for topic in run.subscription.topics[:10]
                ),
            )
        progress = ResearchProgress(
            collected=tuple(topics[topic.casefold()] for topic in parameters.collected),
            missing=tuple(topics[topic.casefold()] for topic in parameters.missing),
            issues=parameters.issues,
        )
        return ToolResult.success(
            {
                "progress": progress.model_dump(mode="json"),
                "observation": "模型报告的材料进度；是否交付由正文证据和正式提交检查决定。",
            }
        )

    async def query_history(self, context, parameters):
        items, total = self.store.history(context, parameters.days, parameters.limit)
        data = {
            "items": items,
            "days": parameters.days,
            "as_of": context.window_end.isoformat(),
            "omitted_count": total - len(items),
            "observation": "比较事件实体、产品版本、日期和关键事实；相同事实避免重复，"
            "有实质新进展时说明变化。历史引用用于比较，新新闻仍需当前任务的正文证据。",
        }
        result = ToolResult.success(data)
        while len(result.model_dump_json()) > self.settings.max_tool_result_chars and items:
            items.pop()
            data["omitted_count"] = total - len(items)
            result = ToolResult.success(data)
        if (total and not items) or len(
            result.model_dump_json()
        ) > self.settings.max_tool_result_chars:
            raise ToolFailure("RESULT_TOO_LARGE", "历史结果无法容纳，请缩小查询范围。")
        return result


def register_research(registry, store, settings):
    tools = ResearchTools(store, settings)
    registry.register(
        ToolDefinition(
            "query_history",
            "查询当前用户已保存简报中的近期事件、关键事实、来源引用和交付时间。"
            "days默认7、最多30；limit默认10、最多20。基准时间固定为本任务window_end；"
            "事实相同避免重复，有新进展说明变化；旧来源ID不能替代当前新闻证据。",
            HistoryParameters,
            tools.query_history,
            retry_safe=True,
        )
    )
    registry.register(
        ToolDefinition(
            "update_progress",
            "更新已取得材料的话题collected、待补充话题missing和当前问题issues三个短列表。"
            "话题来自订阅，已取得与待补充不可重叠；整组替换，进度只辅助决策，不代表完成。",
            ResearchProgress,
            tools.update_progress,
            on_completed=store.on_progress,
        )
    )
