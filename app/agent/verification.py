"""Classify publication metadata without treating missing time as false content."""


def publication_display(publication, window_start, window_end):
    if publication.status == "outside_window" or (
        publication.published_at is not None
        and not window_start <= publication.published_at <= window_end
    ):
        return False, "已知发布时间在本次新闻窗口之外，可作为补充待核实信息列出。"
    if publication.status == "confirmed" and publication.published_at is not None:
        return True, None
    notes = {
        "missing": "发布时间未确认：原文缺少明确发布时间，无法确认是否属于本次24小时新闻窗口。",
        "ambiguous": "发布时间未确认：原文时间缺少时区、精度不足或格式不明确，请结合原文核实。",
        "conflicting": "发布时间有冲突：原文包含不同时间信息，无法确认本次新闻窗口，请查看原文。",
    }
    if publication.status in notes:
        return True, notes[publication.status]
    return False, "来源时间记录不完整，请补查或移入待核实信息。"
