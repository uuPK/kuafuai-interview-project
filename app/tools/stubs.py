"""Read-only offline search/fetch fixtures, explicitly marked in every result."""

from datetime import timedelta
from typing import Literal

from pydantic import Field

from app.tools.context import ToolContext
from app.tools.models import ToolFailure, ToolParameters, ToolResult
from app.tools.registry import CompletionStatus, ToolDefinition, ToolRegistry


class SearchParameters(ToolParameters):
    query: str = Field(min_length=1, max_length=300)
    language: Literal["zh", "en"] = "zh"
    domains: tuple[str, ...] = Field(default=(), max_length=10)
    limit: int = Field(default=5, ge=1, le=10)


class FetchParameters(ToolParameters):
    candidate_id: str = Field(min_length=1, max_length=100)


class FixtureDraft(ToolParameters):
    title: str = Field(min_length=1, max_length=100)
    items: tuple[str, ...] = Field(default=(), max_length=5)


class SubmitFixtureParameters(ToolParameters):
    draft: FixtureDraft


async def submit_fixture(context: ToolContext, parameters: SubmitFixtureParameters) -> ToolResult:
    if parameters.draft.title == "simulate_rejection":
        raise ToolFailure("BRIEF_REJECTED", "替身检查发现问题，请修改测试稿件后提交。")
    return ToolResult.success(
        {"fixture": True, "accepted": True, "draft": parameters.draft.model_dump(mode="json")}
    )


def fixture_completion(result: ToolResult) -> CompletionStatus | None:
    if result.ok and isinstance(result.data, dict) and result.data.get("accepted") is True:
        return "completed"
    return None


def register_submission_fixture(registry: ToolRegistry) -> None:
    """Explicit opt-in: this submission fixture performs no real news quality checks."""
    registry.register(
        ToolDefinition(
            name="submit_brief",
            description="离线提交替身，只保存测试稿件并结束测试任务；不执行正式新闻质量检查。",
            parameters=SubmitFixtureParameters,
            handler=submit_fixture,
            completion_status=fixture_completion,
        )
    )


async def search_fixture(context: ToolContext, parameters: SearchParameters) -> ToolResult:
    if parameters.query == "simulate_failure":
        raise ToolFailure("SEARCH_UNAVAILABLE", "替身搜索暂不可用。", retryable=True)
    items = []
    if parameters.query != "zero_results" and (
        not parameters.domains or "example.com" in parameters.domains
    ):
        items = [
            {
                "candidate_id": "fixture-model",
                "title": "测试新闻：开源模型发布",
                "url": "https://example.com/fixture-model",
                "summary": "固定离线材料。",
                "published_at": (context.window_end - timedelta(hours=1)).isoformat(),
                "language": parameters.language,
            }
        ][: parameters.limit]
    return ToolResult.success({"fixture": True, "items": items})


async def fetch_fixture(context: ToolContext, parameters: FetchParameters) -> ToolResult:
    if parameters.candidate_id != "fixture-model":
        raise ToolFailure("CANDIDATE_NOT_FOUND", "替身候选不存在，请使用搜索返回的 ID。")
    return ToolResult.success(
        {
            "fixture": True,
            "source_id": "fixture-source",
            "candidate_id": parameters.candidate_id,
            "text": "测试原文：某开源模型发布，参数规模为 8B。这是固定离线材料。",
            "published_at": (context.window_end - timedelta(hours=1)).isoformat(),
        }
    )


def build_stub_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="web_search",
            description="离线搜索替身，返回标记 fixture 的固定候选；不联网。",
            parameters=SearchParameters,
            handler=search_fixture,
            budget_dimension="searches",
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch_url",
            description="离线抓取替身，只读取固定候选 ID 的测试原文；不联网。",
            parameters=FetchParameters,
            handler=fetch_fixture,
            budget_dimension="fetches",
        )
    )
    return registry
