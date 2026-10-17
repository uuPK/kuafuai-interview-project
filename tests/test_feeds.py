import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from html import escape
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.main import create_app
from app.providers.feeds import FeedCatalog, FeedClient, FeedSource, parse_feed, published_time
from app.providers.public_http import PublicHttpClient, PublicPage
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.materials import MaterialStore
from app.storage.models import BudgetCharge, RunRequest, StateChange
from app.storage.runs import RunNotFoundError, RunStore
from app.storage.sources import SourceStore
from app.tools.context import bind_context
from app.tools.discovery import CandidateMaterial
from app.tools.executor import ToolExecutor
from app.tools.feed import FeedParameters, FeedTool, register_feed
from app.tools.fetch import register_fetch
from app.tools.files import register_file_tools
from app.tools.models import ToolFailure, ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.stubs import register_submission_fixture
from tests.test_agent_runner import observation, submit
from tests.test_fetch import Pages, context
from tests.test_harness import harness as harness


def source(feed_id="official", **fields):
    return FeedSource(
        feed_id=feed_id,
        name=fields.pop("name", "官方 AI"),
        url=fields.pop("url", "https://feeds.example.com/" + feed_id),
        **fields,
    )


def entry(
    title="AI 模型🙂",
    url="https://news.example.com/new",
    published=None,
    summary="正文摘要",
    *,
    extra="",
    guid=False,
):
    link = (
        f'<guid isPermaLink="true">{escape(url)}</guid>' if guid else f"<link>{escape(url)}</link>"
    )
    date = f"<pubDate>{escape(published)}</pubDate>" if published is not None else ""
    return (
        f"<item><title>{escape(title)}</title>{link}{date}"
        f"<description>{escape(summary)}</description>{extra}</item>"
    )


def rss(*entries, updated="", language="en"):
    return (
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>官方</title><language>{language}</language><lastBuildDate>{updated}</lastBuildDate>{''.join(entries)}</channel></rss>"
    ).encode()


def parse(body, config=None):
    return parse_feed(
        PublicPage("https://feeds.example.com/feed.xml", body, ()),
        source(),
        config or Settings(_env_file=None),
    )


def call(call_id="feed", **args):
    return ToolInvocation(
        tool_call_id=call_id,
        name="read_feed",
        arguments=json.dumps({"feed_id": "official", **args}, ensure_ascii=False),
    )


def install(env, body, *, config=None, handler=None, catalog=None):
    config = config or env.settings
    seen = []

    def respond(request):
        seen.append(request)
        return (
            handler(request)
            if handler
            else httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})
        )

    http = PublicHttpClient(config, transport=httpx.MockTransport(respond))
    catalog = catalog or FeedCatalog(feeds=(source(),))
    store, registry = CandidateStore(env.calls), ToolRegistry()
    register_feed(registry, FeedClient(http, config), store, config, catalog)
    return SimpleNamespace(
        http=http,
        store=store,
        registry=registry,
        executor=ToolExecutor(registry, env.calls, config),
        seen=seen,
        catalog=catalog,
        config=config,
    )


def test_rss_parses_unicode_markup_guid_and_date_clues():
    body = rss(
        entry(published="Tue, 13 Oct 2026 01:00:00 +0000", summary="<b>8 GB</b> 条件", guid=True)
    )
    response = parse(body)
    item = response.items[0]
    assert item.title == "AI 模型🙂" and item.summary == "8 GB 条件"
    assert (
        item.discovery_method == "read_feed"
        and item.feed_id == "official"
        and item.language == "en"
    )
    assert published_time(item.date_clues) == datetime(2026, 10, 13, 1, tzinfo=UTC)
    assert response.received_count == 1 and response.invalid_count == 0


def test_atom_published_updated_xhtml_language_and_relative_base():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en"
      xml:base="https://example.com/blog/"><updated>2026-10-13T09:00:00Z</updated>
      <entry><title>AI</title><link rel="self" href="/api/article"/><link href="new"/>
      <published>2026-10-13T08:00:00+08:00</published><updated>2026-10-13T09:00:00Z</updated>
      <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>8 GB</p></div>
      </summary></entry></feed>"""
    response = parse(body)
    item = response.items[0]
    assert item.url == "https://example.com/blog/new" and item.summary == "8 GB"
    assert item.date_clues == {
        "atom_published": "2026-10-13T08:00:00+08:00",
        "atom_updated": "2026-10-13T09:00:00Z",
    }
    assert published_time(item.date_clues) == datetime(2026, 10, 13, tzinfo=UTC)
    assert response.feed_dates == {"atom_feed_updated": "2026-10-13T09:00:00Z"}


@pytest.mark.parametrize(
    "dates",
    [
        {},
        {"rss_pubDate": None},
        {"rss_pubDate": ""},
        {"rss_pubDate": "2026-10-13"},
        {"rss_pubDate": "2026-10-13T08:00:00"},
        {"rss_pubDate": "Tue, 13 Oct 2026 08:00:00 -0000"},
        {"atom_updated": "2026-10-13T08:00:00Z"},
        {"rss_pubDate": "garbage"},
        {"rss_pubDate": "2026-10-13T08:00:00Z", "rss_dc_date": "2026-10-13T09:00:00Z"},
        {"rss_pubDate": "2026-10-13T08:00:00Z", "rss_dc_date": "bad"},
    ],
)
def test_missing_ambiguous_conflicting_or_update_only_dates_are_not_publication(dates):
    assert published_time(dates) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://10.1.1.1/a",
        "file:///secret",
        "https://u:p@example.com/a",
        "http://[::1]/a",
        "https://example.com/sk-abcdefghijk",
    ],
)
def test_invalid_article_links_are_filtered(url):
    response = parse(rss(entry(url=url)))
    assert not response.items and response.invalid_count == 1


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not XML", "RSS_PARSE_ERROR"),
        (b"<rss>", "RSS_PARSE_ERROR"),
        (b"<html/>", "RSS_FORMAT_UNSUPPORTED"),
        (b'<rss version="0.91"><channel/></rss>', "RSS_FORMAT_UNSUPPORTED"),
        (b'<rss version="2.0"/>', "RSS_FORMAT_UNSUPPORTED"),
        (
            b'<!DOCTYPE rss [<!ENTITY x "safe">]><rss version="2.0"><channel/></rss>',
            "RSS_XML_BLOCKED",
        ),
        (
            b'<!DOCTYPE rss SYSTEM "file:///secret"><rss version="2.0"><channel/></rss>',
            "RSS_XML_BLOCKED",
        ),
        (
            b'<!DOCTYPE rss [<!ENTITY x SYSTEM "http://127.0.0.1/secret">]>'
            b'<rss version="2.0"><channel><item>&x;</item></channel></rss>',
            "RSS_XML_BLOCKED",
        ),
        (
            b'<rss version="2.0" xmlns:xi="http://www.w3.org/2001/XInclude"><channel><xi:include href="file:///secret"/></channel></rss>',
            "RSS_XML_BLOCKED",
        ),
    ],
)
def test_xml_errors_and_external_io_are_blocked(body, code):
    with pytest.raises(ToolFailure, match=code):
        parse(body)


def test_utf16_dtd_and_parser_size_limits():
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY x "safe">]>'
        '<rss version="2.0"><channel/></rss>'
    )
    with pytest.raises(ToolFailure, match="RSS_XML_BLOCKED"):
        parse(xml.encode("utf-16"))
    with pytest.raises(ToolFailure, match="RSS_TOO_LARGE"):
        parse(rss(), Settings(_env_file=None, max_download_bytes=5))
    with pytest.raises(ToolFailure, match="RSS_ENTRY_LIMIT"):
        parse(rss(entry(), entry()), Settings(_env_file=None, max_rss_entries=1))


def test_feed_date_is_separate_and_raw_content_redacted():
    response = parse(
        rss(
            entry(
                summary="fixture-secret sk-abcdefghijklmnop",
                extra="<dc:date>2026-10-13T01:00:00Z</dc:date>",
            ),
            updated="2026-10-13T12:00:00Z",
        ),
        Settings(_env_file=None, deepseek_api_key="fixture-secret"),
    )
    assert response.items[0].date_clues["rss_pubDate"] is None
    assert published_time(response.items[0].date_clues) == datetime(2026, 10, 13, 1, tzinfo=UTC)
    assert "fixture-secret" not in json.dumps(
        response.raw
    ) and "sk-abcdefghijklmnop" not in json.dumps(response.raw)
    assert response.feed_dates["rss_lastBuildDate"] == "2026-10-13T12:00:00Z"


def test_language_and_encoded_summary_are_redacted_and_resolved_url_checked():
    config = Settings(_env_file=None, deepseek_api_key="fixture-secret")
    response = parse(rss(entry(summary="&amp; fixture-secret"), language="fixture-secret"), config)
    assert response.items[0].language == "[REDACTED]"
    assert response.items[0].summary == "& [REDACTED]"
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"
      xml:base="https://example.com/sk-abcdefghijk/">
      <entry><title>AI</title><link href="new"/></entry></feed>"""
    assert parse(body).invalid_count == 1


@pytest.mark.parametrize(
    "item_xml",
    [
        '<item><title>AI</title><guid isPermaLink="false">https://example.com/a</guid></item>',
        "<item><link>https://example.com/a</link></item>",
        "<item><title> </title><link>https://example.com/a</link></item>",
        "<item><title>AI</title></item>",
    ],
)
def test_missing_required_clues_and_non_permalink_guid_are_invalid(item_xml):
    assert parse(rss(item_xml)).invalid_count == 1


@pytest.mark.parametrize("link", ['rel="self"', 'type="application/json"'])
def test_atom_non_article_links_are_invalid(link):
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI</title>'
        f'<link {link} href="https://example.com/a"/></entry></feed>'
    ).encode()
    assert parse(body).invalid_count == 1
    assert not parse(b'<feed xmlns="http://www.w3.org/2005/Atom"/>').items


def test_utf16_success_and_equivalent_date_clues():
    body = '<?xml version="1.0" encoding="UTF-16"?>' + rss(entry()).decode()
    assert parse(body.encode("utf-16")).items[0].title == "AI 模型🙂"
    assert published_time(
        {
            "rss_pubDate": "Tue, 13 Oct 2026 08:00:00 +0800",
            "rss_dc_date": "2026-10-13T00:00:00Z",
        }
    ) == datetime(2026, 10, 13, tzinfo=UTC)


def test_catalog_entry_count_and_legacy_candidate_compatibility():
    with pytest.raises(ValidationError):
        FeedCatalog(feeds=tuple(source(f"source-{index}") for index in range(21)))
    legacy = CandidateMaterial.model_validate_json('{"title":"AI","url":"https://example.com/a"}')
    assert legacy.discovery_method == "web_search" and legacy.feed_id is None
    for update in ({"discovery_method": "read_feed"}, {"feed_id": "official"}):
        with pytest.raises(ValidationError):
            CandidateMaterial.model_validate({**legacy.model_dump(), **update})


@pytest.mark.parametrize(
    "payload",
    [
        {"feeds": [{"feed_id": "x", "name": "n", "url": "http://127.0.0.1/"}]},
        {"feeds": [{"feed_id": "x", "name": "n", "url": "https://example.com", "enabled": "true"}]},
        {"feeds": [{"feed_id": "../x", "name": "n", "url": "https://example.com"}]},
        {"feeds": [{"feed_id": "x", "name": "\n", "url": "https://example.com"}]},
        {"feeds": [{"feed_id": "x", "name": "n", "url": "https://example.com/sk-abcdefghijk"}]},
        {"feeds": [{"feed_id": "x", "name": "n", "url": "https://example.com"}] * 2},
        {"feeds": [], "unknown": 1},
    ],
)
def test_source_catalog_validation_is_safe(tmp_path, payload):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="RSS source catalog") as error:
        FeedCatalog.load(Settings(_env_file=None, rss_sources_file=path))
    assert "127.0.0.1" not in str(error.value) and "sk-abcdefghijk" not in str(error.value)


def test_catalog_is_loaded_once_only_enabled_names_are_exposed(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {"feeds": [source().model_dump(), source("disabled", enabled=False).model_dump()]}
        ),
        encoding="utf-8",
    )
    config = Settings(
        _env_file=None, data_dir=tmp_path / "data", rss_sources_file=path, rss_enabled=True
    )
    catalog = FeedCatalog.load(config)
    assert catalog.public() == ({"feed_id": "official", "name": "官方 AI"},)
    assert catalog.get("disabled") is None
    path.write_text('{"feeds":[]}', encoding="utf-8")
    assert catalog.get("official") is not None and not FeedCatalog.load(config).feeds
    with TestClient(create_app(config)) as client:
        assert (
            client.app.state.tool_registry.get("read_feed")
            and not client.app.state.feed_catalog.feeds
        )
    assert not FeedCatalog.load(Settings(_env_file=None)).feeds


@pytest.mark.parametrize("mode", ["missing", "invalid", "oversize"])
def test_invalid_configured_catalog_prevents_startup(tmp_path, mode):
    path = tmp_path / "sources.json"
    if mode != "missing":
        path.write_bytes(b"x" * 100001 if mode == "oversize" else b"{broken")
    with (
        pytest.raises(ValueError, match="RSS source catalog"),
        TestClient(
            create_app(
                Settings(
                    _env_file=None,
                    data_dir=tmp_path / "data",
                    rss_sources_file=path,
                    rss_enabled=True,
                )
            )
        ),
    ):
        pass


@pytest.mark.parametrize("configured", [False, True])
def test_disabled_rss_does_not_load_catalog_or_expose_tool(tmp_path, configured):
    path = tmp_path / "broken.json"
    path.write_text("not a feed catalog", encoding="utf-8")
    config = Settings(
        _env_file=None, data_dir=tmp_path / "data", rss_sources_file=path if configured else None
    )
    assert config.rss_enabled is False
    with TestClient(create_app(config)) as client:
        assert client.get("/health").status_code == 200
        registry = client.app.state.tool_registry
        assert registry.get("web_search") and registry.get("fetch_url")
        assert registry.get("read_feed") is None and registry.available_feeds == ()
        assert not client.app.state.feed_catalog.feeds


@pytest.mark.parametrize(
    "fields",
    [
        {"feed_id": "https://example.com"},
        {"feed_id": "official", "limit": 0},
        {"feed_id": "official", "limit": 11},
        {"feed_id": "official", "limit": True},
        {"feed_id": "official", "url": "https://example.com"},
        {"feed_id": "official", "user_id": "other"},
    ],
)
def test_feed_parameters_are_strict(fields):
    with pytest.raises(ValidationError):
        FeedParameters.model_validate(fields)


def test_read_feed_persists_reuses_budget_and_sources(tool_env):
    async def run():
        env = tool_env
        recent = format_datetime(env.run.window_end - timedelta(minutes=1))
        setup = install(
            env,
            rss(
                entry(published=recent),
                entry(published=recent),
                entry(url="https://news.example.com/other", published=recent),
            ),
        )
        async with setup.http:
            first = await setup.executor.execute(env.run.id, "user-1", call(limit=1))
            assert (
                first.ok
                and first.data["filtered"]["duplicate_url"] == 1
                and first.data["omitted_count"] == 1
            )
            assert await setup.executor.execute(env.run.id, "user-1", call(limit=1)) == first
            second = await setup.executor.execute(env.run.id, "user-1", call("again"))
            assert (
                second.ok
                and second.data["items"][0]["candidate_id"]
                == first.data["items"][0]["candidate_id"]
            )
            assert second.data["reused_count"] == 2 and len(setup.seen) == 2
            candidate = setup.store.get_candidate(
                env.run.id, "user-1", first.data["items"][0]["candidate_id"]
            )
            assert (
                candidate.material.discovery_method == "read_feed"
                and candidate.material.feed_id == "official"
            )
            sources = SourceStore(setup.store, env.settings)
            register_fetch(setup.registry, Pages(env), sources, env.settings)
            fetched = await setup.executor.execute(
                env.run.id,
                "user-1",
                ToolInvocation(
                    tool_call_id="fetch",
                    name="fetch_url",
                    arguments=json.dumps({"candidate_id": candidate.candidate_id}),
                ),
            )
            assert fetched.ok and fetched.data["usable_in_window"]
            usage = env.runs.get_run(env.run.id, "user-1").budget_usage
            assert usage.searches == 2 and usage.fetches == 1 and usage.tool_calls == 3
            assert (
                "Authorization" not in setup.seen[0].headers
                and "Cookie" not in setup.seen[0].headers
            )
            assert "application/rss+xml" in setup.seen[0].headers["accept"]

    asyncio.run(run())


def test_rss_reuses_search_candidate_and_preserves_first_provenance(tool_env):
    async def run():
        from tests.test_fetch import install as seed_search

        env = tool_env
        search = await seed_search(env, Pages(env))
        setup = install(env, rss(entry()))
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call())
            assert result.ok and result.data["reused_count"] == 1
            item = result.data["items"][0]
            assert (
                item["candidate_id"] == search.ids[0]
                and item["discovery_method"] == "web_search"
                and item["feed_id"] is None
            )
            assert (
                result.data["feed_id"] == "official"
                and result.data["discovery_method"] == "read_feed"
            )

    asyncio.run(run())


def test_search_after_feed_and_repeated_search_reuse_candidate(tool_env):
    async def run():
        from app.providers.bocha import BochaClient
        from app.tools.search import register_search
        from tests.test_bocha import item, settings, wire

        env = tool_env
        setup = install(env, rss(entry()))
        async with setup.http:
            first = await setup.executor.execute(env.run.id, "user-1", call())
        async with BochaClient(
            settings(env.settings.data_dir),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=wire(item()))),
        ) as bocha:
            register_search(setup.registry, bocha, setup.store, env.settings)
            for call_id in ("search", "again-search"):
                result = await setup.executor.execute(
                    env.run.id,
                    "user-1",
                    ToolInvocation(
                        tool_call_id=call_id,
                        name="web_search",
                        arguments='{"query":"AI"}',
                    ),
                )
                assert result.ok and result.data["reused_count"] == 1
                candidate = result.data["items"][0]
                assert candidate["candidate_id"] == first.data["items"][0]["candidate_id"]
                assert candidate["discovery_method"] == "read_feed"
                assert candidate["feed_id"] == "official"
                assert result.data["discovery_method"] == "web_search"
        assert len(setup.store.known_urls(env.run.id, "user-1")) == 1
        usage = env.runs.get_run(env.run.id, "user-1").budget_usage
        assert usage.searches == usage.tool_calls == 3 and usage.fetches == 0

    asyncio.run(run())


def test_old_future_missing_and_ambiguous_dates(tool_env):
    async def run():
        env = tool_env
        old = format_datetime(env.run.window_start - timedelta(seconds=1))
        future = format_datetime(env.run.window_end + timedelta(hours=1))
        setup = install(
            env,
            rss(
                entry(url="https://example.com/old", published=old),
                entry(url="https://example.com/future", published=future),
                entry(url="https://example.com/missing"),
                entry(url="https://example.com/ambiguous", published="2026-10-13"),
            ),
        )
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call())
            assert (
                result.ok
                and result.data["filtered"]["old"] == result.data["filtered"]["future"] == 1
            )
            assert (
                len(result.data["items"]) == 2
                and result.data["items"][0]["date_clues"]["rss_pubDate"] is None
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["empty", "old", "invalid", "disabled", "unknown", "extra", "limit"]
)
def test_empty_feed_and_dependency_or_permission_problems_are_distinct(tool_env, mode):
    async def run():
        env = tool_env
        body = (
            rss()
            if mode == "empty"
            else rss(entry(published=format_datetime(env.run.window_start - timedelta(days=1))))
            if mode == "old"
            else rss(entry(url="http://127.0.0.1/"))
        )
        setup = install(env, body, catalog=FeedCatalog(feeds=(source(enabled=mode != "disabled"),)))
        args = (
            {"feed_id": "missing"}
            if mode == "unknown"
            else {"url": "https://example.com"}
            if mode == "extra"
            else {"limit": 11}
            if mode == "limit"
            else {}
        )
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call(**args))
            if mode in {"empty", "old"}:
                assert result.ok and result.data["items"] == []
            else:
                assert result.error.code == (
                    "RSS_INVALID_ENTRIES"
                    if mode == "invalid"
                    else "INVALID_ARGUMENTS"
                    if mode in {"extra", "limit"}
                    else "RSS_FEED_NOT_AVAILABLE"
                )
                assert len(setup.seen) == (1 if mode == "invalid" else 0)

    asyncio.run(run())


def test_sizes_and_owner_isolation(tool_env):
    async def run():
        env = tool_env
        body = rss(
            entry(
                title="AI",
                summary="长" * 900,
            ),
            entry(url="https://example.com/second", summary="长" * 900),
        )
        config = env.settings.model_copy(update={"max_tool_result_chars": 1600})
        setup = install(env, body, config=config)
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call())
            assert (
                result.ok
                and len(result.model_dump_json()) <= 1600
                and result.data["omitted_count"] > 0
            )
            assert result.data["items"][0]["text_truncated"]
            denied = await setup.executor.execute(env.run.id, "other-user", call("foreign"))
            assert denied.error.code == "PERMISSION_DENIED" and len(setup.seen) == 1
            with pytest.raises(RunNotFoundError):
                setup.store.known_urls(env.run.id, "other-user")
        setup = install(
            env, rss(entry()), config=env.settings.model_copy(update={"max_tool_result_chars": 256})
        )
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call("small"))
            assert (
                result.error.code == "RESULT_TOO_LARGE"
                and setup.store.discovery(env.run.id, "user-1", "small") is None
            )

    asyncio.run(run())


def test_excluded_keywords_and_mixed_invalid_rows(tool_env):
    async def run():
        env = tool_env
        env.run = env.runs.create_run(
            RunRequest(
                user_id="user-1",
                subscription_id="sub-2",
                subscription=env.run.subscription.model_copy(
                    update={"exclude_keywords": ("AdVeRt",)}
                ),
            )
        )
        env.run = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
        setup = install(
            env,
            rss(
                entry(summary="advert promotion"),
                entry(url="http://127.0.0.1/a"),
                entry(url="https://example.com/good"),
            ),
        )
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call())
            assert result.ok and len(result.data["items"]) == 1
            assert result.data["filtered"]["excluded"] == result.data["filtered"]["invalid"] == 1
            assert result.data["items"][0]["url"] == "https://example.com/good"
            unbound = bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1")
            with pytest.raises(PermissionError):
                await FeedTool(
                    FeedClient(setup.http, env.settings), setup.catalog, setup.store, env.settings
                ).execute(unbound, FeedParameters(feed_id="official"))
            assert len(setup.seen) == 1

    asyncio.run(run())


def test_foreign_candidate_reference_rolls_back_discovery_and_event(tool_env):
    async def run():
        env = tool_env
        setup = install(env, rss(entry()))
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call())
        other = env.runs.create_run(
            RunRequest(
                user_id="user-2",
                subscription_id="sub-2",
                subscription=env.run.subscription,
            )
        )
        env.runs.transition(other.id, "user-2", StateChange(status="running"))
        env.calls.prepare_calls(other.id, "user-2", [call("cross-run")])
        env.calls.start_call(
            other.id, "user-2", "cross-run", BudgetCharge(tool_calls=1, searches=1)
        )
        before = env.runs.list_events(other.id, "user-2")
        with pytest.raises(PermissionError, match="referenced candidate"):
            setup.store.save(
                other.id,
                "user-2",
                "cross-run",
                provider="rss",
                provider_request_id=None,
                raw={},
                candidates=(),
                result=result,
                references=(result.data["items"][0]["candidate_id"],),
            )
        assert setup.store.discovery(other.id, "user-2", "cross-run") is None
        assert not setup.store.known_urls(other.id, "user-2")
        assert env.runs.list_events(other.id, "user-2") == before

    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_failure_and_transient_retry(tool_env, status):
    async def run():
        env = tool_env
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return (
                httpx.Response(status) if count == 1 else httpx.Response(200, content=rss(entry()))
            )

        setup = install(
            env,
            b"",
            handler=handler,
            config=env.settings.model_copy(update={"retry_base_seconds": 0.001}),
        )
        async with setup.http:
            result = await setup.executor.execute(env.run.id, "user-1", call())
            assert result.ok if status != 403 else result.error.code == "FETCH_FORBIDDEN"
            assert len(setup.seen) == (1 if status == 403 else 2)
            assert env.runs.get_run(env.run.id, "user-1").budget_usage.searches == 1

    asyncio.run(run())


def test_feed_redirect_cannot_target_local_network(tool_env):
    async def run():
        setup = install(
            tool_env,
            b"",
            handler=lambda request: httpx.Response(
                302, headers={"location": "http://127.0.0.1/secret"}
            ),
        )
        async with setup.http:
            result = await setup.executor.execute(tool_env.run.id, "user-1", call())
            assert result.error.code == "PUBLIC_URL_BLOCKED" and len(setup.seen) == 1

    asyncio.run(run())


def test_snapshot_survives_feedback_failure_and_database_reopen(tool_env):
    async def run():
        env = tool_env
        setup = install(env, rss(entry()))

        def fail(connection, record):
            raise RuntimeError("feedback interrupted")

        async with setup.http:
            with pytest.raises(RuntimeError):
                await setup.executor.execute(env.run.id, "user-1", call(), on_finished=fail)
        saved = setup.store.discovery(env.run.id, "user-1", "feed")
        assert saved is not None and env.calls.get_call(env.run.id, "user-1", "feed").result is None
        database = Database(env.database.path)
        database.initialize()
        env.runs = RunStore(database, env.settings)
        env.calls = CallStore(env.runs)
        reopened = install(env, b"broken", catalog=FeedCatalog())
        try:
            result = await reopened.registry.get("read_feed").handler(
                context(env, "feed"), FeedParameters(feed_id="official")
            )
            assert result == saved.result and not reopened.seen
            reopened.executor.finish(env.run.id, "user-1", call(), result)
            assert await reopened.executor.execute(env.run.id, "user-1", call()) == result
        finally:
            await reopened.http.aclose()

    asyncio.run(run())


def test_rate_limit_waits_and_recovers_same_feed(harness, response_factory):
    env = harness()
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"retry-after": "8"})
            if count == 1
            else httpx.Response(200, content=rss(entry()))
        )

    async def run():
        setup = install(env, b"", handler=handler)
        register_submission_fixture(setup.registry)
        async with setup.http:
            waiting = await AgentRunner(
                ScriptedModel([response_factory(("feed", "read_feed", {"feed_id": "official"}))]),
                setup.executor,
                env.conversations,
                env.settings,
            ).run(env.run.id, "user-1")
            assert waiting.status == "waiting_retry"
            env.clock.advance(8)
            database = Database(env.database.path)
            database.initialize()
            env.runs = RunStore(database, env.settings, clock=env.clock.now)
            env.calls = CallStore(env.runs)
            env.conversations = ConversationStore(env.calls)
            recovered = install(env, b"", handler=handler)
            register_submission_fixture(recovered.registry)
            async with recovered.http:
                done = await AgentRunner(
                    ScriptedModel([submit(response_factory)]),
                    recovered.executor,
                    env.conversations,
                    env.settings,
                ).run(env.run.id, "user-1")
                assert done.status == "completed" and done.budget_usage.searches == 1
                assert (
                    done.window_end == waiting.window_end
                    and done.deadline_at == waiting.deadline_at
                )
                assert (
                    recovered.executor.requests.for_tool(env.run.id, "user-1", "feed").attempts == 2
                )

    asyncio.run(run())


@pytest.mark.parametrize("first_status", [200, 403])
def test_model_chooses_another_feed_after_feedback_and_reads_source(
    tool_env, response_factory, first_status
):
    async def run():
        env = tool_env
        catalog = FeedCatalog(feeds=(source("old"), source("official")))

        def handler(request):
            if request.url.path.endswith("old") and first_status != 200:
                return httpx.Response(first_status)
            return httpx.Response(
                200, content=rss() if request.url.path.endswith("old") else rss(entry())
            )

        setup = install(env, b"", catalog=catalog, handler=handler)
        sources = SourceStore(setup.store, env.settings)
        register_fetch(setup.registry, Pages(env), sources, env.settings)
        register_file_tools(setup.registry, MaterialStore(sources, env.settings), env.settings)
        register_submission_fixture(setup.registry)
        conversations = ConversationStore(env.calls)

        def change(request):
            task = json.loads(request.messages[1].content)
            assert (
                task["available_feeds"] == list(catalog.public())
                and "https://feeds" not in request.messages[1].content
            )
            name, result = observation(request)
            assert name == "read_feed"
            if first_status == 200:
                assert result.ok and not result.data["items"]
            else:
                assert not result.ok and result.error.code == "FETCH_FORBIDDEN"
            return response_factory(("official", "read_feed", {"feed_id": "official"}))

        def fetch(request):
            _, result = observation(request)
            return response_factory(
                ("fetch", "fetch_url", {"candidate_id": result.data["items"][0]["candidate_id"]})
            )

        def read(request):
            _, result = observation(request)
            return response_factory(("read", "read_file", {"path": result.data["file_path"]}))

        def finish(request):
            _, result = observation(request)
            assert result.ok and result.data["source_id"] and result.data["content"]
            return submit(response_factory)

        async with setup.http:
            model = ScriptedModel(
                [
                    response_factory(("old", "read_feed", {"feed_id": "old"})),
                    change,
                    fetch,
                    read,
                    finish,
                ]
            )
            done = await AgentRunner(model, setup.executor, conversations, env.settings).run(
                env.run.id, "user-1"
            )
            assert (
                done.status == "completed"
                and done.budget_usage.searches == 2
                and done.budget_usage.fetches == 1
            )
            ModelRequest(
                messages=conversations.messages(env.run.id, "user-1"),
                tools=tuple(setup.registry.schemas()),
            )

    asyncio.run(run())
