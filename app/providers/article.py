"""Offline HTML extraction into canonical text, Unicode offsets, and explicit date evidence."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version

import trafilatura
from lxml import etree, html
from pydantic import AwareDatetime, Field
from trafilatura.settings import use_config
from trafilatura.utils import decode_file

from app.harness.redaction import Redactor
from app.tools.models import ToolFailure, ToolParameters


class TextBlock(ToolParameters):
    kind: str
    title: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)


class TextIndex(ToolParameters):
    offset_unit: str = "unicode_characters"
    range_convention: str = "[start_offset, end_offset)"
    blocks: tuple[TextBlock, ...]
    sections: tuple[TextBlock, ...]


class Publication(ToolParameters):
    status: str
    published_at: AwareDatetime | None = None
    modified_at: tuple[str, ...] = ()
    evidence: tuple[dict, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class Article:
    title: str
    language: str | None
    body: str
    index: TextIndex
    publication: Publication
    extraction_version: str


def _timestamp(value):
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def publication_evidence(tree, inferred, discovery_dates, window_start, window_end):
    evidence = []
    published_names = {
        "article:published_time",
        "og:published_time",
        "datepublished",
        "pubdate",
        "publishdate",
    }
    modified_names = {"article:modified_time", "og:updated_time", "datemodified", "last-modified"}
    for element in tree.xpath("//meta"):
        name = (
            element.get("property") or element.get("name") or element.get("itemprop") or ""
        ).lower()
        if name in published_names | modified_names:
            evidence.append(
                {
                    "kind": "published" if name in published_names else "modified",
                    "value": element.get("content", ""),
                    "locator": f"meta:{name}",
                }
            )
    for element in tree.xpath("//time[@datetime]"):
        if element.get("itemprop", "").lower() == "datepublished" or "pubdate" in element.attrib:
            evidence.append(
                {
                    "kind": "published",
                    "value": element.get("datetime"),
                    "locator": "time:datePublished",
                }
            )
    for script in tree.xpath('//script[@type="application/ld+json"]')[:20]:
        try:
            decoded = json.loads(script.text or "")
        except (ValueError, RecursionError):
            continue

        def visit(value, depth=0):
            if depth > 10:
                return
            if isinstance(value, list):
                for child in value[:100]:
                    visit(child, depth + 1)
            elif isinstance(value, dict):
                types = value.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and any(
                    name in {"Article", "NewsArticle", "BlogPosting", "TechArticle", "Report"}
                    for name in types
                    if isinstance(name, str)
                ):
                    for key, kind in (("datePublished", "published"), ("dateModified", "modified")):
                        if isinstance(value.get(key), str):
                            evidence.append(
                                {"kind": kind, "value": value[key], "locator": f"jsonld:{key}"}
                            )
                for child in list(value.values())[:100]:
                    if isinstance(child, (dict, list)):
                        visit(child, depth + 1)

        visit(decoded)
    if inferred:
        evidence.append({"kind": "inferred", "value": inferred, "locator": "trafilatura:date"})
    evidence.extend(
        {"kind": "discovery", "value": value, "locator": key}
        for key, value in discovery_dates.items()
    )
    explicit = [item["value"] for item in evidence if item["kind"] == "published"]
    parsed = [_timestamp(value) for value in explicit]
    precise = set(value for value in parsed if value is not None)
    published_at = next(iter(precise)) if len(precise) == 1 else None
    if len(precise) > 1:
        status, issue = "conflicting", "原文包含相互冲突的发布时间，请补查可靠来源。"
    elif explicit and (not precise or any(value is None for value in parsed)):
        status, issue = (
            "ambiguous",
            "原文发布时间缺少时区、精度不足或格式不明确，不能确认新闻窗口。",
        )
    elif not precise:
        status, issue = "missing", "原文缺少明确发布时间；搜索或提取日期线索不足以确认新闻窗口。"
    elif not window_start <= published_at <= window_end:
        status, issue = "outside_window", "原文发布时间不在本任务固定新闻窗口内，请换来源或选题。"
    else:
        status, issue = "confirmed", None
    return Publication(
        status=status,
        published_at=published_at,
        modified_at=tuple(
            dict.fromkeys(item["value"] for item in evidence if item["kind"] == "modified")
        ),
        evidence=tuple(evidence),
        issues=(issue,) if issue else (),
    )


def render_blocks(root, title, redactor):
    values = []
    for element in root.iter():
        tag = element.tag
        if tag not in {"head", "p", "item", "row"}:
            continue
        if any(parent.tag in {"p", "item", "row"} for parent in element.iterancestors()):
            continue
        if tag == "row":
            text = "\t".join(
                " ".join(" ".join(cell.itertext()).split())
                for cell in element
                if cell.tag == "cell"
            )
        else:
            text = " ".join(" ".join(element.itertext()).split())
            if tag == "item":
                text = "- " + text if text else ""
        if text.strip():
            values.append(
                (
                    "heading" if tag == "head" else "table_row" if tag == "row" else "paragraph",
                    redactor.clean(text),
                )
            )
    if not values:
        raise ToolFailure("FETCH_EMPTY_BODY", "网页未提取到有效正文，请换来源。")
    if title and (values[0][1] != title):
        values.insert(0, ("title", title))
    body, blocks = "", []
    for kind, text in values:
        if body:
            body += "\n\n"
        start = len(body)
        body += text
        blocks.append(
            TextBlock(kind=kind, title=text[:120], start_offset=start, end_offset=len(body))
        )
    body += "\n"
    headings = [block for block in blocks if block.kind == "heading"]
    if headings:
        starts = [block for block in blocks if block.kind in {"title", "heading"}]
        sections = tuple(
            block.model_copy(
                update={
                    "end_offset": starts[index + 1].start_offset
                    if index + 1 < len(starts)
                    else len(body)
                }
            )
            for index, block in enumerate(starts)
        )
    else:
        sections = tuple(
            block.model_copy(
                update={"title": block.title if block.kind == "title" else f"段落 {index + 1}"}
            )
            for index, block in enumerate(blocks)
        )
    return body, TextIndex(blocks=tuple(blocks), sections=sections)


def extract_article(page, candidate, context, redactor: Redactor):
    try:
        decoded = decode_file(page.body, max_size=len(page.body))
        tree = html.fromstring(
            decoded.encode("utf-8"),
            parser=html.HTMLParser(encoding="utf-8", no_network=True, recover=True),
        )
        config = deepcopy(use_config())
        config.set("DEFAULT", "MAX_TREE_SIZE", "100000")
        document = trafilatura.bare_extraction(
            deepcopy(tree),
            url=page.url,
            fast=True,
            favor_recall=True,
            include_comments=False,
            include_tables=True,
            with_metadata=True,
            deduplicate=False,
            config=config,
            date_extraction_params={"extensive_search": False, "original_date": True},
        )
        if document is None or document.body is None:
            raise ToolFailure("FETCH_EMPTY_BODY", "网页未提取到有效正文，请换来源。")
        declared_titles = tree.xpath("//meta[@property='og:title']/@content")
        declared_titles += tree.xpath("//title/text()")
        title = next(
            (
                line.strip()
                for value in declared_titles
                for line in value.splitlines()
                if line.strip()
            ),
            candidate.title,
        )
        title = redactor.clean(" ".join(title.split()))[:2000]
        body, index = render_blocks(document.body, title, redactor)
        dates = publication_evidence(
            tree, document.date, candidate.date_clues, context.window_start, context.window_end
        )
        # Scrub before the canonical hash/index are created; no later body rewriting is allowed.
        dates = Publication.model_validate_json(
            json.dumps(redactor.clean(dates.model_dump(mode="json")), ensure_ascii=False)
        )
        return Article(
            title=title,
            language=redactor.clean(document.language or tree.get("lang")),
            body=body,
            index=index,
            publication=dates,
            extraction_version="trafilatura-" + version("trafilatura") + ":text-v1",
        )
    except (etree.ParserError, etree.XMLSyntaxError, ValueError, TypeError):
        raise ToolFailure("FETCH_EXTRACTION", "网页内容无法安全提取，请换来源。") from None
