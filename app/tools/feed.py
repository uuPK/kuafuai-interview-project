"""Discover bounded feed candidates using the shared search budget and replay store."""

from uuid import uuid4

from pydantic import Field

from app.providers.feeds import FeedCatalog, published_time
from app.storage.candidates import CandidateRecord
from app.tools.models import ToolFailure, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition


class FeedParameters(ToolParameters):
    feed_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    limit: int = Field(default=5, ge=1, le=10)


class FeedTool:
    def __init__(self, client, catalog, store, settings):
        self.client, self.catalog, self.store, self.settings = client, catalog, store, settings

    async def execute(self, context, parameters):
        if context.tool_call_id is None:
            raise PermissionError("RSS requires server-bound call")
        cached = self.store.discovery(context.run_id, context.user_id, context.tool_call_id)
        if cached is not None:
            return cached.result
        feed = self.catalog.get(parameters.feed_id)
        if feed is None:
            raise ToolFailure(
                "RSS_FEED_NOT_AVAILABLE",
                "来源未配置或已禁用，请选择 available_feeds 中的来源或使用搜索。",
            )
        response = await self.client.read(feed)
        if response.received_count and response.invalid_count == response.received_count:
            raise ToolFailure("RSS_INVALID_ENTRIES", "RSS 条目缺少有效标题或公开链接，请换来源。")
        excluded = self.store.runs.get_run(
            context.run_id, context.user_id
        ).subscription.exclude_keywords
        filtered = {
            "invalid": response.invalid_count,
            "excluded": 0,
            "duplicate_url": 0,
            "old": 0,
            "future": 0,
        }
        accepted, new, references, seen = [], [], [], set()
        known = self.store.known_urls(context.run_id, context.user_id)
        for material in response.items:
            text = (material.title + "\n" + (material.summary or "")).casefold()
            if any(word.casefold() in text for word in excluded):
                filtered["excluded"] += 1
                continue
            published = published_time(material.date_clues)
            if published is not None and published < context.window_start:
                filtered["old"] += 1
                continue
            if published is not None and published > context.window_end:
                filtered["future"] += 1
                continue
            if material.url in seen:
                filtered["duplicate_url"] += 1
                continue
            seen.add(material.url)
            candidate = known.get(material.url)
            if candidate is None:
                candidate = CandidateRecord(
                    candidate_id="cand-" + uuid4().hex,
                    run_id=context.run_id,
                    tool_call_id=context.tool_call_id,
                    position=len(new),
                    material=material,
                )
                new.append(candidate)
            else:
                references.append(candidate.candidate_id)
            accepted.append(candidate)
        items = [
            {
                "candidate_id": candidate.candidate_id,
                "title": candidate.material.title[:300],
                "url": candidate.material.url,
                "summary": (candidate.material.summary or candidate.material.snippet or "")[:800],
                "language": candidate.material.language,
                "date_clues": candidate.material.date_clues,
                "discovery_method": candidate.material.discovery_method,
                "feed_id": candidate.material.feed_id,
                "text_truncated": len(candidate.material.title) > 300
                or len(candidate.material.summary or candidate.material.snippet or "") > 800,
            }
            for candidate in accepted[: parameters.limit]
        ]
        data = {
            "provider": "rss",
            "discovery_method": "read_feed",
            "feed_id": feed.feed_id,
            "feed_name": feed.name,
            "items": items,
            "received_count": response.received_count,
            "filtered": filtered,
            "reused_count": len(references),
            "omitted_count": len(accepted) - len(items),
            "feed_dates": response.feed_dates,
            "observation": "RSS 摘要和日期仅为发现线索；需抓取正文核实发布时间和事实。"
            if items
            else "有效清单没有当前窗口内可用候选，可换来源或继续搜索；缺失/含糊的日期不自动补造。",
        }
        result = ToolResult.success(data)
        while len(result.model_dump_json()) > self.settings.max_tool_result_chars and items:
            items.pop()
            data["omitted_count"] = len(accepted) - len(items)
            result = ToolResult.success(data)
        if (accepted and not items) or len(
            result.model_dump_json()
        ) > self.settings.max_tool_result_chars:
            raise ToolFailure(
                "RESULT_TOO_LARGE", "RSS 结果超过大小边界，请减少条数或选择其他来源。"
            )
        return self.store.save(
            context.run_id,
            context.user_id,
            context.tool_call_id,
            provider="rss",
            provider_request_id=None,
            raw=response.raw,
            candidates=tuple(new),
            references=tuple(references),
            result=result,
        ).result


def register_feed(registry, client, store, settings, catalog=None):
    catalog = catalog if catalog is not None else FeedCatalog.load(settings)
    registry.register(
        ToolDefinition(
            "read_feed",
            "读取 available_feeds 中已启用的官方 RSS/Atom 来源。"
            "只接受 feed_id；limit 默认5、最多10；与搜索共用发现预算。"
            "摘要/日期仅为候选线索，需fetch_url核实正文；"
            "无结果或来源失败时可换来源或搜索。",
            FeedParameters,
            FeedTool(client, catalog, store, settings).execute,
            budget_dimension="searches",
            retry_safe=True,
        )
    )
    registry.available_feeds = catalog.public()
