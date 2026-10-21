"""Deterministic delivery rules; factual meaning is reviewed separately in step 3.2."""

from dataclasses import dataclass

from app.agent.briefs import (
    BriefDocument,
    PublishedCitation,
    PublishedItem,
    PublishedUnverifiedItem,
)
from app.agent.verification import publication_display
from app.harness.redaction import Redactor
from app.storage.snapshots import SourceMetadata
from app.tools.models import ArgumentIssue, ToolFailure, ToolResult


@dataclass(frozen=True)
class CheckedBrief:
    document: BriefDocument
    sources: tuple[SourceMetadata, ...]
    duplicate_hints: tuple[dict, ...]
    citation_adjustments: tuple[dict, ...] = ()


class BriefRules:
    def __init__(self, sources, settings):
        self.sources, self.settings = sources, settings
        self.redactor = Redactor(settings)

    def check(self, context, draft, *, enrich_pending=True, auto_locate=True):
        run = self.sources.runs.get_run(context.run_id, context.user_id)
        problems = []

        def issue(field, explanation):
            problems.append(ArgumentIssue(field=field, issue=explanation))

        if self.redactor.clean(draft.model_dump(mode="json")) != draft.model_dump(mode="json"):
            issue("draft", "稿件含敏感内容，移除凭据或认证信息后重新提交")
        if len(draft.items) > run.subscription.max_items:
            issue("draft.items", "条数超过本任务订阅上限")
        if len(draft.items) < run.subscription.max_items and not draft.shortfall_reason:
            issue("draft.shortfall_reason", "新闻不足设定条数时需要说明原因")
        topics = {topic.casefold() for topic in run.subscription.topics}
        covered, missing = set(), set()
        for i, item in enumerate(draft.items):
            names = [topic.casefold() for topic in item.topics]
            if len(set(names)) != len(names) or not set(names) <= topics:
                issue(f"draft.items[{i}].topics", "话题必须来自订阅且不能重复")
            covered.update(names)
        for i, note in enumerate(draft.uncovered_topics):
            name = note.topic.casefold()
            if name not in topics or name in missing or name in covered:
                issue(f"draft.uncovered_topics[{i}].topic", "未覆盖话题须来自订阅且不重复或已覆盖")
            missing.add(name)
        if topics - covered - missing:
            issue("draft.uncovered_topics", "每个订阅话题都需入选内容或未覆盖原因")

        exclusions = tuple(word.casefold() for word in run.subscription.exclude_keywords)
        if any(word in draft.title.casefold() for word in exclusions):
            issue("draft.title", "简报标题命中排除关键词")
        loaded, published, used, scanned = {}, [], {}, 0
        adjustments = []
        for i, item in enumerate(draft.items):
            citations, seen, notes = [], set(), []
            item_text = "\n".join(
                (
                    item.title,
                    item.summary,
                    item.selection_reason,
                    *item.key_facts,
                    *item.entities,
                    item.product_version or "",
                    *(citation.quote for citation in item.citations),
                )
            )
            if any(word in item_text.casefold() for word in exclusions):
                issue(f"draft.items[{i}]", "新闻字段或引用命中排除关键词")
            for j, citation in enumerate(item.citations):
                field = f"draft.items[{i}].citations[{j}]"
                if citation.source_id not in loaded:
                    try:
                        meta, index = self.sources.get_source(
                            context.run_id, context.user_id, citation.source_id
                        )
                    except LookupError:
                        issue(field + ".source_id", "来源不存在或不属于当前任务")
                        continue
                    if meta.run_id != context.run_id or meta.source_id != citation.source_id:
                        raise ToolFailure("SOURCE_STATE_PROBLEM", "来源元数据与任务归属不一致。")
                    body = self.sources.files.verify(context, meta, index)
                    scanned += len(body.encode("utf-8"))
                    if scanned > self.settings.max_material_scan_bytes:
                        raise ToolFailure(
                            "BRIEF_EVIDENCE_TOO_LARGE", "引用材料合计超过读取边界，请缩小稿件。"
                        )
                    loaded[citation.source_id] = meta, body
                meta, body = loaded[citation.source_id]
                used.setdefault(meta.source_id, set()).add(i)
                publication = meta.publication
                eligible, note = publication_display(
                    publication, context.window_start, context.window_end
                )
                if not eligible:
                    issue(field + ".source_id", note)
                    continue
                if note and note not in notes:
                    notes.append(note)
                in_range = 0 <= citation.start_offset < citation.end_offset <= len(body)
                if (
                    not in_range
                    or body[citation.start_offset : citation.end_offset] != citation.quote
                ):
                    exact_start = body.find(citation.quote)
                    if (
                        auto_locate
                        and exact_start >= 0
                        and body.find(citation.quote, exact_start + 1) < 0
                    ):
                        adjustments.append(
                            {
                                "field": field,
                                "source_id": meta.source_id,
                                "original_start": citation.start_offset,
                                "original_end": citation.end_offset,
                                "start_offset": exact_start,
                                "end_offset": exact_start + len(citation.quote),
                            }
                        )
                        citation = citation.model_copy(
                            update={
                                "start_offset": exact_start,
                                "end_offset": exact_start + len(citation.quote),
                            }
                        )
                    elif not in_range:
                        issue(field, "引用范围须为正文内非空字符区间[start_offset,end_offset)")
                        continue
                if body[citation.start_offset : citation.end_offset] != citation.quote:
                    actual = body[citation.start_offset : citation.end_offset]
                    mismatch = next(
                        (
                            position
                            for position, (left, right) in enumerate(
                                zip(citation.quote, actual, strict=False)
                            )
                            if left != right
                        ),
                        min(len(citation.quote), len(actual)),
                    )
                    exact_start = body.find(citation.quote)
                    if exact_start >= 0 and body.find(citation.quote, exact_start + 1) < 0:
                        issue(
                            field + ".quote",
                            (
                                f"字符{citation.start_offset + mismatch}：完整quote唯一定位"
                                f"start_offset={exact_start},"
                                f"end_offset={exact_start + len(citation.quote)}；"
                                "保留原quote"
                            )[:80],
                        )
                        continue
                    expected_char = (
                        repr(citation.quote[mismatch]) if mismatch < len(citation.quote) else "结束"
                    )
                    actual_char = repr(actual[mismatch]) if mismatch < len(actual) else "结束"
                    issue(
                        field + ".quote",
                        (
                            f"字符{citation.start_offset + mismatch}：quote为{expected_char}，"
                            f"正文为{actual_char}；"
                            "用完整quote定位并更新起止范围，勿复用旧位置"
                        )[:80],
                    )
                    continue
                key = (meta.source_id, citation.start_offset, citation.end_offset)
                if key in seen:
                    issue(field, "同一条新闻包含重复引用")
                seen.add(key)
                if any(word in (meta.title + "\n" + body).casefold() for word in exclusions):
                    issue(field + ".source_id", "引用来源标题或正文命中排除关键词")
                citations.append(
                    PublishedCitation(
                        **citation.model_dump(),
                        url=meta.url,
                        content_hash=meta.content_hash,
                        published_at=publication.published_at,
                        publication_status=publication.status,
                    )
                )
            if len(citations) == len(item.citations):
                published.append(
                    PublishedItem(
                        **{
                            **item.model_dump(),
                            "citations": tuple(citations),
                            "verification_notes": tuple(notes),
                        }
                    )
                )

        pending, seen_candidates = [], set()
        main_candidates = {meta.candidate_id for meta, _ in loaded.values()}
        for i, item in enumerate(draft.unverified_items):
            field = f"draft.unverified_items[{i}]"
            names = [topic.casefold() for topic in item.topics]
            if len(set(names)) != len(names) or not set(names) <= topics:
                issue(field + ".topics", "话题必须来自订阅且不能重复")
            if item.candidate_id in seen_candidates or item.candidate_id in main_candidates:
                issue(field + ".candidate_id", "候选不能重复，或同时出现在主新闻与待核实列表")
                continue
            seen_candidates.add(item.candidate_id)
            try:
                candidate = self.sources.candidates.get_candidate(
                    context.run_id, context.user_id, item.candidate_id
                )
            except LookupError:
                issue(field + ".candidate_id", "待核实来源不存在或不属于当前任务")
                continue
            material = candidate.material
            if self.redactor.contains_credential(material.url):
                issue(field + ".candidate_id", "待核实来源地址含敏感信息，不能展示")
                continue
            summary = item.summary
            if summary is None and enrich_pending:
                summary = self.pending_summary(context, candidate, material.url)
            pending_text = "\n".join((material.title, item.reason, summary or "", *item.topics))
            if any(word in pending_text.casefold() for word in exclusions):
                issue(field, "待核实信息命中排除关键词")
                continue
            pending.append(
                PublishedUnverifiedItem(
                    **{**item.model_dump(), "summary": summary},
                    title=material.title.strip()[:300] or "待核实来源",
                    url=material.url,
                )
            )

        if not draft.items and not draft.unverified_items:
            self._empty_evidence(context, issue)
        if problems:
            return None, self.rejection(problems)
        groups = {}
        for source_id, (meta, _) in loaded.items():
            for kind, value in (("same_url", meta.url), ("same_body", meta.content_hash)):
                groups.setdefault((kind, value), set()).update(used[source_id])
        hints = tuple(
            {"kind": kind, "items": sorted(indices)}
            for (kind, _), indices in groups.items()
            if len(indices) > 1
        )
        document = BriefDocument(
            **{**draft.model_dump(), "items": tuple(published), "unverified_items": tuple(pending)}
        )
        return CheckedBrief(
            document, tuple(meta for meta, _ in loaded.values()), hints, tuple(adjustments)
        ), None

    def pending_summary(self, context, candidate, url):
        """Use owned saved material only; excerpt fallback is explicitly unverified."""
        if candidate.material.url != url:
            return None
        text = candidate.material.summary or candidate.material.snippet
        prefix = "原文检索摘要（尚未核实）："
        if not text or not text.strip():
            with self.sources.database.connection() as connection:
                row = connection.execute(
                    "SELECT id FROM sources WHERE run_id=? "
                    "AND json_extract(metadata_json,'$.candidate_id')=? ORDER BY id LIMIT 1",
                    (context.run_id, candidate.candidate_id),
                ).fetchone()
            if row is None:
                return None
            meta, index = self.sources.get_source(context.run_id, context.user_id, row[0])
            text = self.sources.files.verify(context, meta, index)
            prefix = "原文节选（尚未核实）："
        text = self.redactor.clean(text.strip())
        return prefix + text[:1900] if text else None

    def rejection(self, problems, *, code="BRIEF_RULES_REJECTED", message=None):
        details = tuple(problems[:10])
        result = ToolResult.failure(
            code, message or "提交检查未通过，请根据details修改稿件。", details=details
        )
        while len(result.model_dump_json()) > self.settings.max_tool_result_chars and details:
            details = details[:-1]
            result = ToolResult.failure(
                code,
                "提交规则未通过，请修改稿件或缩小提交范围。",
                details=details,
            )
        return result

    def _empty_evidence(self, context, issue):
        with self.sources.database.connection() as connection:
            discoveries = connection.execute(
                """SELECT d.result_json FROM discoveries d JOIN tool_calls c
                    ON c.run_id=d.run_id AND c.tool_call_id=d.tool_call_id
                    WHERE d.run_id=? AND c.status='completed'""",
                (context.run_id,),
            ).fetchall()
            calls = connection.execute(
                """SELECT name,result_json FROM tool_calls WHERE run_id=? AND status='completed'
                    AND name IN ('web_search','read_feed','fetch_url')""",
                (context.run_id,),
            ).fetchall()
            candidates = connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE run_id=?", (context.run_id,)
            ).fetchone()[0]
        try:
            discovered = [ToolResult.model_validate_json(row[0]) for row in discoveries]
            results = [
                (row["name"], ToolResult.model_validate_json(row["result_json"])) for row in calls
            ]
            if any(
                not result.ok
                or not isinstance(result.data, dict)
                or not isinstance(result.data.get("items"), list)
                for result in discovered
            ):
                raise ValueError("invalid saved discovery")
        except (ValueError, TypeError):
            raise ToolFailure(
                "DISCOVERY_STATE_PROBLEM", "已保存的检索结果损坏，不能据此确认无新闻。"
            ) from None
        if not discovered:
            issue("draft.items", "空简报需要本任务已保存的有效搜索或RSS结果")
        search_succeeded = any(name == "web_search" and result.ok for name, result in results)
        if any(
            not result.ok and not (name == "read_feed" and search_succeeded)
            for name, result in results
        ):
            issue("draft.items", "搜索或抓取存在未成功调用，不能将依赖/材料失败标记为暂无新闻")
        if candidates and not any(name == "fetch_url" and result.ok for name, result in results):
            issue("draft.items", "已有候选但未取得正文，不能确认暂无符合条件的新闻")
