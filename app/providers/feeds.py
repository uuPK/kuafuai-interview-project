"""Trusted feed catalog and bounded RSS 2.0/Atom parsing with no XML I/O."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin

from lxml import etree
from pydantic import Field, ValidationError, field_validator

from app.harness.redaction import Redactor
from app.storage.snapshots import digest
from app.tools.discovery import CandidateMaterial, candidate_url
from app.tools.models import ToolFailure, ToolParameters

ATOM = "{http://www.w3.org/2005/Atom}"
XML = "{http://www.w3.org/XML/1998/namespace}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


class FeedSource(ToolParameters):
    feed_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        return candidate_url(value)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("feed name must be plain nonempty text")
        return value.strip()


class FeedCatalog(ToolParameters):
    feeds: tuple[FeedSource, ...] = Field(default=(), max_length=20)

    @field_validator("feeds")
    @classmethod
    def unique_ids(cls, feeds):
        if len({feed.feed_id for feed in feeds}) != len(feeds):
            raise ValueError("feed IDs must be unique")
        return feeds

    @classmethod
    def load(cls, settings):
        if settings.rss_sources_file is None:
            return cls()
        try:
            with settings.rss_sources_file.open("rb") as file:
                raw = file.read(100_001)
            if len(raw) > 100_000:
                raise ValueError("catalog too large")
            catalog = cls.model_validate_json(raw)
            if Redactor(settings).clean(catalog.model_dump(mode="json")) != catalog.model_dump(
                mode="json"
            ):
                raise ValueError("catalog contains credentials")
            return catalog
        except (OSError, ValueError, ValidationError):
            raise ValueError(
                "RSS source catalog is missing, invalid or contains sensitive data"
            ) from None

    def get(self, feed_id):
        return next((feed for feed in self.feeds if feed.feed_id == feed_id and feed.enabled), None)

    def public(self):
        return tuple(
            {"feed_id": feed.feed_id, "name": feed.name} for feed in self.feeds if feed.enabled
        )


@dataclass(frozen=True)
class FeedResponse:
    items: tuple[CandidateMaterial, ...]
    received_count: int
    invalid_count: int
    feed_dates: dict[str, str | None]
    raw: dict = field(repr=False)


def published_time(clues):
    values = [
        value
        for key, value in clues.items()
        if key in {"rss_pubDate", "rss_dc_date", "atom_published"} and value
    ]
    dates = []
    for value in values:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is not None:
                dates.append(parsed.astimezone(UTC))
        except (ValueError, TypeError, OverflowError):
            pass
    return dates[0] if dates and len(dates) == len(values) and len(set(dates)) == 1 else None


class _DenyResolver(etree.Resolver):
    def resolve(self, url, public_id, context):
        raise ToolFailure("RSS_XML_BLOCKED", "RSS XML 不允许加载外部实体或文件。")


def _text(element, tag):
    found = element.find(tag)
    return "".join(found.itertext()).strip() if found is not None else None


def _summary(text):
    # Strip feed markup without parsing HTML, resolving links or loading extra resources.
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", text or ""))).strip()


def parse_feed(page, feed, settings):
    if len(page.body) > settings.max_download_bytes:
        raise ToolFailure("RSS_TOO_LARGE", "RSS 下载超过大小限制。")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        attribute_defaults=False,
        huge_tree=False,
        recover=False,
    )
    parser.resolvers.add(_DenyResolver())
    try:
        root = etree.fromstring(page.body, parser, base_url=page.url)
    except (etree.XMLSyntaxError, ValueError):
        raise ToolFailure("RSS_PARSE_ERROR", "来源未返回可解析的 RSS/Atom 清单。") from None
    if root.getroottree().docinfo.doctype or any(
        str(node.tag).startswith("{http://www.w3.org/2001/XInclude}") for node in root.iter()
    ):
        raise ToolFailure("RSS_XML_BLOCKED", "RSS XML 不允许 DTD、实体声明或 XInclude。")
    redactor = Redactor(settings)
    if root.tag == "rss" and root.get("version") == "2.0" and root.find("channel") is not None:
        container = root.find("channel")
        entries = container.findall("item")
        atom = False
        feed_dates = {
            "rss_lastBuildDate": _text(container, "lastBuildDate"),
            "rss_feed_pubDate": _text(container, "pubDate"),
        }
    elif root.tag == ATOM + "feed":
        container, atom = root, True
        entries = root.findall(ATOM + "entry")
        feed_dates = {"atom_feed_updated": _text(root, ATOM + "updated")}
    else:
        raise ToolFailure("RSS_FORMAT_UNSUPPORTED", "来源不是支持的 RSS 2.0 或 Atom 清单。")
    if len(entries) > settings.max_rss_entries:
        raise ToolFailure("RSS_ENTRY_LIMIT", "RSS 条目超过解析数量限制，请选择较小来源。")
    materials, invalid = [], 0
    for entry in entries:
        title = _text(entry, (ATOM if atom else "") + "title") or ""
        if atom:
            links = [
                node
                for node in entry.findall(ATOM + "link")
                if node.get("rel", "alternate") == "alternate"
                and node.get("type", "text/html") in {"text/html", "application/xhtml+xml"}
            ]
            link = links[0] if links else None
            url = link.get("href") if link is not None else None
            clues = {
                "atom_published": _text(entry, ATOM + "published"),
                "atom_updated": _text(entry, ATOM + "updated"),
            }
            summary = _text(entry, ATOM + "summary") or _text(entry, ATOM + "content")
        else:
            link = entry.find("link")
            url = _text(entry, "link")
            if not url:
                link = entry.find("guid")
                url = (
                    _text(entry, "guid")
                    if link is not None and link.get("isPermaLink", "true").lower() == "true"
                    else None
                )
            clues = {
                "rss_pubDate": _text(entry, "pubDate"),
                "rss_dc_date": _text(entry, DC + "date"),
            }
            summary = _text(entry, "description") or _text(entry, CONTENT + "encoded")
        language = entry.get(XML + "lang") or root.get(XML + "lang") or _text(container, "language")
        try:
            if not url or redactor.contains_credential(url):
                raise ValueError("missing or sensitive URL")
            url = candidate_url(urljoin(link.base or page.url, url))
            if redactor.contains_credential(url):
                raise ValueError("sensitive resolved URL")
            materials.append(
                CandidateMaterial(
                    title=redactor.clean(title),
                    url=url,
                    summary=redactor.clean(_summary(summary)),
                    site_name=feed.name,
                    language=redactor.clean(language),
                    date_clues=redactor.clean(clues),
                    discovery_method="read_feed",
                    feed_id=feed.feed_id,
                )
            )
        except (ValueError, UnicodeError):
            invalid += 1
    raw = redactor.clean(
        {
            "feed_id": feed.feed_id,
            "url": page.url,
            "redirects": list(page.redirects),
            "body_hash": digest(page.body),
            "xml": etree.tostring(root, encoding="unicode"),
            "feed_dates": feed_dates,
        }
    )
    return FeedResponse(tuple(materials), len(entries), invalid, redactor.clean(feed_dates), raw)


class FeedClient:
    def __init__(self, public_http, settings):
        self.http, self.settings = public_http, settings

    async def read(self, feed):
        page = await self.http.get(
            feed.url,
            accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
            html_only=False,
        )
        return parse_feed(page, feed, self.settings)
