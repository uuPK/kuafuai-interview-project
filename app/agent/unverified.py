"""A rejected draft may yield bounded original-source leads, without asserted news facts."""

from app.agent.briefs import BriefDraft, UncoveredTopic, UnverifiedItem


def unverified_draft(
    draft,
    checked,
    issues,
    topics,
    *,
    include_summaries=True,
    keep_indices=(),
    preserve_pending=False,
):
    sources = {source.source_id: source for source in checked.sources}
    candidates, seen = [], set()

    def add(candidate_id, field, item):
        if candidate_id in seen:
            return
        seen.add(candidate_id)
        relevant = [
            issue.issue
            for issue in issues
            if issue.field == field or issue.field.startswith(field + ".")
        ]
        relevant.extend(issue.issue for issue in issues if issue.field in {"draft", "draft.title"})
        reason = (
            "最终事实核对仍有问题，以下简介尚未核实。"
            if include_summaries
            else "最终事实核对仍有问题，本条仅保留原文线索。"
        )
        reason += (
            "核对问题：" + "；".join(dict.fromkeys(relevant))
            if relevant
            else "本轮整份稿件仍有未解决的核对问题，未作为已确认主新闻交付。"
        )
        if len(reason) > 500:
            reason = reason[:480] + "…完整问题见任务核对反馈。"
        candidates.append(
            UnverifiedItem(
                candidate_id=candidate_id,
                reason=reason,
                summary=item.summary if include_summaries else None,
                topics=item.topics if include_summaries else (),
            )
        )

    for i, item in enumerate(draft.items):
        if i in keep_indices:
            continue
        add(sources[item.citations[0].source_id].candidate_id, f"draft.items[{i}]", item)
    for i, item in enumerate(draft.unverified_items):
        if preserve_pending and item.candidate_id not in seen:
            seen.add(item.candidate_id)
            candidates.append(item)
            continue
        add(item.candidate_id, f"draft.unverified_items[{i}]", item)
    if not candidates:
        return None
    retained = tuple(draft.items[i] for i in keep_indices)
    covered = {topic.casefold() for item in retained for topic in item.topics}
    return BriefDraft(
        title=draft.title if retained else "本次待核实原文线索",
        items=retained,
        unverified_items=tuple(candidates[:5]),
        uncovered_topics=tuple(
            UncoveredTopic(
                topic=topic,
                reason="本话题的候选仍待核实，未计入主新闻"
                if retained
                else "最终事实核对仍有问题，本次仅交付待核实原文线索",
            )
            for topic in topics
            if topic.casefold() not in covered
        ),
        shortfall_reason=(
            f"本次保留{len(retained)}条通过原文核对的新闻，其余候选仍待核实，不计入主新闻条数。"
            if retained
            else "本次未确认主新闻，待核实来源不计入新闻条数。"
        )
        + ("候选较多，仅展示前5个原文来源。" if len(candidates) > 5 else ""),
    )


def retained_indices(draft, checked, outcome):
    """Only explicit positive item decisions may survive a negative overall review."""
    import re

    if outcome.item_reviews is None or any(
        not re.match(r"^draft\.items\[\d+\](?:\.|$)", issue.field) for issue in outcome.issues
    ):
        return ()
    keep = {item.item_index for item in outcome.item_reviews if item.approved}
    sources = {source.source_id: source.candidate_id for source in checked.sources}
    # A source cannot be both published and listed as an unverified candidate.
    while True:
        pending = {
            sources[item.citations[0].source_id]
            for i, item in enumerate(draft.items)
            if i not in keep
        }
        updated = {
            i
            for i in keep
            if not any(sources[ref.source_id] in pending for ref in draft.items[i].citations)
        }
        if updated == keep:
            return tuple(sorted(keep))
        keep = updated


def review_notes(document, outcome, indices=None):
    if outcome is None or outcome.item_reviews is None:
        return document
    notes = {item.item_index: item.notes for item in outcome.item_reviews if item.approved}
    indices = range(len(document.items)) if indices is None else indices
    items = tuple(
        item.model_copy(
            update={
                "verification_notes": tuple(
                    dict.fromkeys((*item.verification_notes, *notes.get(index, ())))
                ),
            }
        )
        for index, item in zip(indices, document.items, strict=True)
    )
    return document.model_copy(update={"items": items})
