"""Real web_search: filter discovery links, persist snapshots, return bounded observations."""

from uuid import uuid4

from app.config import Settings
from app.providers.bocha import BochaClient
from app.storage.candidates import CandidateRecord, CandidateStore
from app.tools.context import ToolContext
from app.tools.discovery import SearchParameters, matches_domains
from app.tools.models import ToolFailure, ToolResult
from app.tools.registry import ToolDefinition, ToolRegistry


class SearchTool:
    def __init__(self, client: BochaClient | None, store: CandidateStore, settings: Settings):
        self.client, self.store, self.settings = client, store, settings

    async def execute(self, context: ToolContext, parameters: SearchParameters) -> ToolResult:
        if context.tool_call_id is None:
            raise PermissionError("search requires a server-bound call ID")
        cached = self.store.discovery(context.run_id, context.user_id, context.tool_call_id)
        if cached is not None:
            return cached.result
        if self.client is None:
            raise ToolFailure("BOCHA_CONFIGURATION", "博查服务配置缺失。", category="configuration")
        response = await self.client.search(parameters)
        excluded = self.store.runs.get_run(
            context.run_id, context.user_id
        ).subscription.exclude_keywords
        filtered = {
            "invalid": response.invalid_count,
            "domain": 0,
            "excluded": 0,
            "duplicate_url": 0,
        }
        accepted, new, references, seen = [], [], [], set()
        known = self.store.known_urls(context.run_id, context.user_id)
        for material in response.items:
            if not matches_domains(material.url, parameters.domains):
                filtered["domain"] += 1
                continue
            text = "\n".join(
                (material.title, material.summary or "", material.snippet or "")
            ).casefold()
            if any(keyword.casefold() in text for keyword in excluded):
                filtered["excluded"] += 1
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
        visible = accepted[: parameters.limit]
        items = []
        for candidate in visible:
            material = candidate.material
            summary = material.summary or material.snippet or ""
            items.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "title": material.title[:300],
                    "url": material.url,
                    "summary": summary[:800],
                    "language": material.language,
                    "date_clues": material.date_clues,
                    "discovery_method": material.discovery_method,
                    "feed_id": material.feed_id,
                    "text_truncated": len(summary) > 800 or len(material.title) > 300,
                }
            )
        data = {
            "provider": "bocha",
            "discovery_method": "web_search",
            "items": items,
            "requested_language": parameters.language,
            "freshness": "oneDay",
            "received_count": response.received_count,
            "filtered": filtered,
            "reused_count": len(references),
            "omitted_count": len(accepted) - len(items),
            "provider_request_id": response.provider_request_id,
            "observation": "候选摘要仅用于发现来源；日期和正文仍需抓取核实。"
            if items
            else "有效搜索没有符合当前条件的候选，可调整查询词、语言或域名。",
        }
        result = ToolResult.success(data)
        while len(result.model_dump_json()) > self.settings.max_tool_result_chars and items:
            items.pop()
            data["omitted_count"] = len(accepted) - len(items)
            result = ToolResult.success(data)
        # Do not turn an oversized nonempty discovery into an apparently valid zero result.
        if (visible and not items) or len(
            result.model_dump_json()
        ) > self.settings.max_tool_result_chars:
            raise ToolFailure(
                "RESULT_TOO_LARGE", "搜索结果超过工具大小限制，请减少条数或缩小条件。"
            )
        saved = self.store.save(
            context.run_id,
            context.user_id,
            context.tool_call_id,
            provider="bocha",
            provider_request_id=response.provider_request_id,
            raw=response.raw,
            candidates=tuple(new),
            references=tuple(references),
            result=result,
        )
        return saved.result


def register_search(
    registry: ToolRegistry, client: BochaClient | None, store: CandidateStore, settings: Settings
) -> None:
    registry.register(
        ToolDefinition(
            name="web_search",
            description=(
                "博查搜索候选来源。query 使用实际中文或英文查询词，language 表示查询语言意图，"
                "不保证结果语言。domains 只接受主机名并校验结果属于该域名或子域；"
                "limit 默认 5、最多 10。过去一天仅作粗筛；原始日期与摘要不是正文证据。"
                "使用返回的 candidate_id 抓取正文，零结果时可改词、换语言或域名。"
            ),
            parameters=SearchParameters,
            handler=SearchTool(client, store, settings).execute,
            budget_dimension="searches",
            retry_safe=True,
        )
    )
