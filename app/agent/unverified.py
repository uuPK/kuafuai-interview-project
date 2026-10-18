"""A rejected draft may yield bounded original-source leads, without asserted news facts."""

from app.agent.briefs import BriefDraft, UncoveredTopic, UnverifiedItem


def unverified_draft(draft, checked, issues, topics):
    sources = {source.source_id: source for source in checked.sources}
    candidates, seen = [], set()

    def add(candidate_id, field):
        if candidate_id in seen:
            return
        seen.add(candidate_id)
        relevant = [
            issue.issue
            for issue in issues
            if issue.field == field or issue.field.startswith(field + ".")
        ]
        relevant.extend(issue.issue for issue in issues if issue.field in {"draft", "draft.title"})
        reason = "最终事实核对仍有问题，本条仅保留原文线索。"
        reason += (
            "核对问题：" + "；".join(dict.fromkeys(relevant))
            if relevant
            else "本轮整份稿件仍有未解决的核对问题，未作为已确认主新闻交付。"
        )
        if len(reason) > 500:
            reason = reason[:480] + "…完整问题见任务核对反馈。"
        candidates.append(UnverifiedItem(candidate_id=candidate_id, reason=reason))

    for i, item in enumerate(draft.items):
        add(sources[item.citations[0].source_id].candidate_id, f"draft.items[{i}]")
    for i, item in enumerate(draft.unverified_items):
        add(item.candidate_id, f"draft.unverified_items[{i}]")
    if not candidates:
        return None
    return BriefDraft(
        title="本次待核实原文线索",
        items=(),
        unverified_items=tuple(candidates[:5]),
        uncovered_topics=tuple(
            UncoveredTopic(topic=topic, reason="最终事实核对仍有问题，本次仅交付待核实原文线索")
            for topic in topics
        ),
        shortfall_reason="本次未确认主新闻，待核实来源不计入新闻条数。"
        + ("候选较多，仅展示前5个原文来源。" if len(candidates) > 5 else ""),
    )
