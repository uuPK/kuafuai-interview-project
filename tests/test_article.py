import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from lxml import etree, html

from app.harness.redaction import Redactor
from app.providers.article import extract_article, publication_evidence, render_blocks
from app.providers.public_http import PublicPage
from app.tools.discovery import CandidateMaterial
from app.tools.models import ToolFailure


def article_html(date=None, *, extra="", content=None):
    meta = f'<meta property="article:published_time" content="{date}">' if date else ""
    content = content or (
        "<h1>模型发布测试</h1><p>"
        + "这是原文证据，性能提升 23%，条件为单张 GPU。" * 20
        + "</p><h2>测试条件</h2><p>"
        + "中文字符和 emoji 😀 使用字符位置索引。" * 15
        + "</p><ul><li>许可证：Apache 2.0</li><li>限制：需要 24 GB 显存</li></ul>"
        + "<table><tr><th>测试条件</th><th>吞吐量</th></tr>"
        + "<tr><td>batch=8，GPU=H100</td><td>123 token/s</td></tr></table>"
    )
    return (
        '<html lang="zh"><head><meta charset="utf-8"><title>模型发布测试</title>'
        + meta
        + extra
        + "</head><body><article>"
        + content
        + "</article></body></html>"
    ).encode("utf-8")


def extract(config, body, *, date_clues=None):
    end = datetime(2026, 10, 13, 12, tzinfo=UTC)
    context = SimpleNamespace(window_start=end - timedelta(days=1), window_end=end)
    candidate = CandidateMaterial(
        title="候选标题", url="https://example.com/article", date_clues=date_clues or {}
    )
    return extract_article(
        PublicPage(candidate.url, body, ()), candidate, context, Redactor(config)
    )


def test_real_extraction_preserves_conditions_tables_unicode_offsets(tool_env):
    article = extract(tool_env.settings, article_html("2026-10-13T10:00:00Z"))
    assert article.publication.status == "confirmed"
    assert article.title == "模型发布测试" and article.language == "zh"
    assert "23%" in article.body and "单张 GPU" in article.body
    assert "123 token/s" in article.body and "batch=8" in article.body
    assert "吞吐量" in article.body and "24 GB" in article.body
    assert "\r" not in article.body and article.body.endswith("\n")
    assert article.index.offset_unit == "unicode_characters"
    for block in article.index.blocks:
        assert article.body[block.start_offset : block.end_offset][:120] == block.title
    for section in article.index.sections:
        assert 0 <= section.start_offset < section.end_offset <= len(article.body)
    assert len(article.body.encode("utf-8")) > len(article.body)
    assert article.extraction_version.startswith("trafilatura-")


def test_long_paragraph_is_not_truncated_and_credentials_scrubbed(tool_env):
    text = "正文信息 😀 " * 2000 + " fixture-deep-key "
    article = extract(tool_env.settings, article_html(content=f"<p>{text}</p>"))
    assert len(article.body) > 14000
    assert "fixture-deep-key" not in article.body
    assert article.publication.status == "missing"
    assert len(article.index.blocks) == len(article.index.sections)


@pytest.mark.parametrize("body", [b"", b"<html><body></body></html>", b"<script>ignored</script>"])
def test_empty_or_unusable_html_is_explicit_feedback(tool_env, body):
    with pytest.raises(ToolFailure) as error:
        extract(tool_env.settings, body)
    assert error.value.result.error.code in {"FETCH_EMPTY_BODY", "FETCH_EXTRACTION"}


@pytest.mark.parametrize(
    "dates,status",
    [
        ([], "missing"),
        (["2026-10-13"], "ambiguous"),
        (["2026-10-13T10:00:00"], "ambiguous"),
        (["2026-99-99T10:00:00Z"], "ambiguous"),
        (["2026-10-13T10:00:00Z", "2026-10-13"], "ambiguous"),
        (["2026-10-13T10:00:00Z", "2026-10-13T11:00:00Z"], "conflicting"),
        (["2026-10-13T10:00:00Z", "2026-10-13T18:00:00+08:00"], "confirmed"),
        (["2026-10-10T10:00:00Z"], "outside_window"),
        (["2026-10-14T10:00:00Z"], "outside_window"),
        (["2026-10-12T12:00:00Z"], "confirmed"),
        (["2026-10-13T12:00:00Z"], "confirmed"),
    ],
)
def test_date_precision_conflict_timezone_and_fixed_window(tool_env, dates, status):
    extra = "".join(f'<meta itemprop="datePublished" content="{date}">' for date in dates)
    article = extract(tool_env.settings, article_html(extra=extra))
    assert article.publication.status == status
    assert bool(article.publication.issues) == (status != "confirmed")


def test_modified_discovery_and_inferred_dates_do_not_confirm_publication(tool_env):
    body = article_html(
        extra='<meta property="article:modified_time" content="2026-10-13T10:00:00Z">'
    )
    article = extract(tool_env.settings, body, date_clues={"datePublished": "2026-10-13T10:00:00Z"})
    assert article.publication.status == "missing" and article.publication.published_at is None
    assert article.publication.modified_at == ("2026-10-13T10:00:00Z",)
    assert any(item["kind"] == "discovery" for item in article.publication.evidence)


def test_jsonld_time_tag_and_invalid_json_are_separate_date_evidence(tool_env):
    structured = {
        "@graph": [
            {
                "@type": ["NewsArticle"],
                "datePublished": "2026-10-13T10:00:00Z",
                "dateModified": "2026-10-13T11:00:00Z",
            }
        ]
    }
    extra = '<script type="application/ld+json">broken</script>'
    extra += '<script type="application/ld+json">' + json.dumps(structured) + "</script>"
    body = article_html(
        extra=extra,
        content='<time itemprop="datePublished" datetime="2026-10-13T10:00:00Z"></time>'
        + "<p>"
        + "有效原文 " * 200
        + "</p>",
    )
    article = extract(tool_env.settings, body)
    assert article.publication.status == "confirmed"
    assert article.publication.modified_at == ("2026-10-13T11:00:00Z",)
    assert {item["locator"] for item in article.publication.evidence} >= {
        "jsonld:datePublished",
        "time:datePublished",
    }


def test_render_retains_blocks_without_duplicate_nested_text(tool_env):
    root = etree.fromstring(
        b"<body><head>Heading</head><p>One<hi> bold</hi></p>"
        b"<list><item>Item<p> nested</p></item></list>"
        b"<table><row><cell>A</cell><cell>B</cell></row></table></body>"
    )
    body, index = render_blocks(root, "Title", Redactor(tool_env.settings))
    assert body == "Title\n\nHeading\n\nOne bold\n\n- Item nested\n\nA\tB\n"
    assert len(index.blocks) == 5 and len(index.sections) == 2


def test_jsonld_depth_and_non_article_dates_are_not_accepted():
    end = datetime(2026, 10, 13, 12, tzinfo=UTC)
    nested = {"@type": "NewsArticle", "datePublished": "2026-10-13T10:00:00Z"}
    for _ in range(12):
        nested = {"node": nested}
    tree = html.fromstring(
        '<html><script type="application/ld+json">'
        + json.dumps(nested)
        + '</script><script type="application/ld+json">'
        + '{"@type":"Organization","datePublished":"2026-10-13T10:00:00Z"}'
        + "</script></html>"
    )
    assert (
        publication_evidence(tree, "2026-10-13", {}, end - timedelta(days=1), end).status
        == "missing"
    )


@pytest.mark.parametrize("encoding,declared", [("utf-8", False), ("gb18030", True)])
def test_html_encoding_is_decoded_before_offsets(tool_env, encoding, declared):
    text = "中文原文，测量条件为单张 GPU，吞吐量 123 token/s。" * 100
    body = article_html(content=f"<p>{text}</p>").decode("utf-8")
    body = body.replace(
        '<meta charset="utf-8">', f'<meta charset="{encoding}">' if declared else ""
    )
    article = extract(tool_env.settings, body.encode(encoding))
    assert article.title == "模型发布测试" and text in article.body
    for block in article.index.blocks:
        assert article.body[block.start_offset : block.end_offset][:120] == block.title


def test_html_external_dtd_does_not_supply_article_text(tool_env):
    prefix = b'<!DOCTYPE html SYSTEM "http://127.0.0.1/private.dtd">'
    article = extract(tool_env.settings, prefix + article_html())
    assert "原文证据" in article.body


def test_missing_page_title_uses_candidate_title(tool_env):
    body = article_html(content="<p>" + "完整新闻正文 " * 100 + "</p><h2>相关报道</h2>")
    body = body.replace("<title>模型发布测试</title>".encode(), b"<title></title>")
    article = extract(tool_env.settings, body)
    assert article.title == "候选标题" and article.body.startswith("候选标题\n")


def test_multiline_page_title_uses_declared_first_line(tool_env):
    body = article_html(content="<p>" + "完整新闻正文 " * 100 + "</p><h2>相关报道</h2>")
    body = body.replace(
        "<title>模型发布测试</title>".encode(),
        "<title>真正新闻标题\n正文被网站塞入标题</title>".encode(),
    )
    assert extract(tool_env.settings, body).title == "真正新闻标题"
