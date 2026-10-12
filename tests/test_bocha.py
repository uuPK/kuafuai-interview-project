import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.providers.bocha import BochaClient
from app.tools.discovery import SearchParameters, candidate_url, matches_domains
from app.tools.models import ToolFailure


def settings(path, **overrides):
    return Settings(
        _env_file=None,
        data_dir=path,
        bocha_search_url="https://search.example.com/v1/web-search",
        bocha_api_key="fixture-bocha-key",
        **overrides,
    )


def wire(*items, **fields):
    return {
        "code": 200,
        "log_id": "trace-1",
        "msg": None,
        "data": {"webPages": {"value": list(items)}},
        **fields,
    }


def item(**fields):
    return {"name": "模型发布", "url": "https://news.example.com/new", **fields}


async def search(config, handler, parameters=None):
    async with BochaClient(config, transport=httpx.MockTransport(handler)) as client:
        return await client.search(parameters or SearchParameters(query="AI"))


def test_missing_config_never_leaks_credentials(tmp_path):
    with pytest.raises(ToolFailure) as caught:
        BochaClient(Settings(_env_file=None, data_dir=tmp_path))
    assert caught.value.info.category == "configuration"
    assert "BOCHA_CONFIGURATION" == str(caught.value)


def test_live_contract_sample_and_payload(tmp_path):
    sample = json.loads((Path(__file__).parent / "fixtures/bocha-success.json").read_text("utf-8"))
    received = []

    def handler(request):
        received.append(request)
        assert request.url == "https://search.example.com/v1/web-search"
        assert request.headers["authorization"] == "Bearer fixture-bocha-key"
        assert json.loads(request.content) == {
            "query": "AI",
            "freshness": "oneDay",
            "summary": True,
            "count": 5,
        }
        assert "fixture-bocha-key" not in request.content.decode()
        return httpx.Response(200, json=sample)

    response = asyncio.run(search(settings(tmp_path), handler))
    assert len(received) == 1 and len(response.items) == 2
    assert response.raw == sample
    assert response.provider_request_id == "fixture-live-bocha-request"
    assert response.items[0].language is None
    assert response.items[0].date_clues == {
        "datePublished": "2026-10-12T00:00:00+08:00",
        "dateLastCrawled": "2026-10-12T00:00:00Z",
    }
    assert "published_at" not in response.items[0].model_dump()


@pytest.mark.parametrize("pages", [None, {"value": []}])
def test_valid_empty_response(tmp_path, pages):
    response = asyncio.run(
        search(
            settings(tmp_path),
            lambda req: httpx.Response(
                200,
                json=wire(data={"webPages": pages}),
            ),
        )
    )
    assert response.items == () and response.received_count == 0


def test_missing_optional_fields_stay_missing(tmp_path):
    response = asyncio.run(
        search(settings(tmp_path), lambda req: httpx.Response(200, json=wire(item())))
    )
    material = response.items[0]
    assert material.summary is None and material.snippet is None
    assert material.language is None and material.date_clues == {}


def test_finite_float_extension_is_preserved(tmp_path):
    response = asyncio.run(
        search(settings(tmp_path), lambda req: httpx.Response(200, json=wire(item(), score=0.25)))
    )
    assert response.raw["score"] == 0.25


def test_mixed_invalid_rows_and_raw_dates(tmp_path):
    payload = wire(
        item(datePublished="昨天", dateLastCrawled=None),
        item(url="file:///bad"),
        item(name=""),
        item(datePublished=123),
    )
    response = asyncio.run(
        search(settings(tmp_path), lambda req: httpx.Response(200, json=payload))
    )
    assert response.received_count == 4 and response.invalid_count == 3
    assert response.items[0].date_clues == {"datePublished": "昨天", "dateLastCrawled": None}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"code": True, "data": {"webPages": None}},
        wire(data=None),
        wire(data={}),
        wire(data={"webPages": {}}),
        wire(data={"webPages": {"value": None}}),
        wire(data={"webPages": {"value": ["bad"]}}),
        wire(log_id=123),
        wire(data={"webPages": {"value": [], "totalEstimatedMatches": -1}}),
    ],
)
def test_schema_errors_are_not_successful_zero_results(tmp_path, payload):
    with pytest.raises(ToolFailure, match="BOCHA_RESPONSE_SCHEMA"):
        asyncio.run(search(settings(tmp_path), lambda req: httpx.Response(200, json=payload)))


def test_all_invalid_rows_are_explicit_failure(tmp_path):
    with pytest.raises(ToolFailure, match="BOCHA_INVALID_RESULTS"):
        asyncio.run(
            search(settings(tmp_path), lambda req: httpx.Response(200, json=wire(item(url=""))))
        )


@pytest.mark.parametrize(
    "status,category,code,retryable",
    [
        (400, "protocol", "BOCHA_HTTP_PROTOCOL", False),
        (401, "authentication", "BOCHA_AUTHENTICATION", False),
        (402, "quota", "BOCHA_QUOTA", False),
        (403, "quota", "BOCHA_QUOTA", False),
        (404, "configuration", "BOCHA_ENDPOINT", False),
        (422, "protocol", "BOCHA_HTTP_PROTOCOL", False),
        (429, "transient", "BOCHA_RATE_LIMIT", True),
        (500, "transient", "BOCHA_UNAVAILABLE", True),
        (503, "transient", "BOCHA_UNAVAILABLE", True),
    ],
)
@pytest.mark.parametrize("http_envelope", [True, False])
def test_http_and_application_status_and_safe_diagnostics(
    tmp_path, status, category, code, retryable, http_envelope
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status if http_envelope else 200,
            json={
                "code": status,
                "log_id": "trace-1",
                "msg": "fixture-bocha-key",
                "secret": "private",
                "data": None,
            },
            headers={"retry-after": "8", "x-request-id": "request-2"},
        )

    with pytest.raises(ToolFailure) as caught:
        asyncio.run(search(settings(tmp_path), handler))
    failure = caught.value.info
    assert failure.category == category and failure.code == code
    assert failure.retryable == retryable and failure.retry_after == "8"
    assert len(seen) == 1
    assert failure.diagnostics.provider_request_id == "request-2"
    assert failure.diagnostics.http_status == (status if http_envelope else 200)
    encoded = failure.model_dump_json() + caught.value.result.model_dump_json()
    assert "fixture-bocha-key" not in encoded and "private" not in encoded


def test_redirect_is_not_followed(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://another.example.com"})

    with pytest.raises(ToolFailure, match="BOCHA_ENDPOINT"):
        asyncio.run(search(settings(tmp_path), handler))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ReadTimeout, "BOCHA_TIMEOUT"),
        (httpx.ConnectError, "BOCHA_NETWORK"),
        (httpx.InvalidURL, "BOCHA_ENDPOINT"),
    ],
)
def test_network_errors_never_echo_exception(tmp_path, error, code):
    def handler(request):
        raise error("fixture-bocha-key")

    with pytest.raises(ToolFailure, match=code) as caught:
        asyncio.run(search(settings(tmp_path), handler))
    assert "fixture-bocha-key" not in caught.value.result.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        b"{}{}",
        b'{"code":200,"code":200}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e999}',
        b"\xff",
        b'{"code":',
    ],
)
def test_malformed_json(tmp_path, body):
    with pytest.raises(ToolFailure, match="BOCHA_RESPONSE_JSON"):
        asyncio.run(search(settings(tmp_path), lambda req: httpx.Response(200, content=body)))


@pytest.mark.parametrize("status", [200, 401, 429])
def test_bounded_download_prioritizes_known_http_status(tmp_path, status):
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(
            search(
                settings(tmp_path, max_download_bytes=10),
                lambda req: httpx.Response(
                    status,
                    content=b"x" * 100,
                    headers={"retry-after": "7"},
                ),
            )
        )
    assert (
        caught.value.info.code
        == {
            200: "BOCHA_RESPONSE_TOO_LARGE",
            401: "BOCHA_AUTHENTICATION",
            429: "BOCHA_RATE_LIMIT",
        }[status]
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (200, "BOCHA_TIMEOUT"),
        (401, "BOCHA_AUTHENTICATION"),
        (429, "BOCHA_RATE_LIMIT"),
    ],
)
def test_interrupted_error_body_preserves_status(tmp_path, status, code):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"code":'
            raise httpx.ReadTimeout("fixture-bocha-key")

    with pytest.raises(ToolFailure, match=code) as caught:
        asyncio.run(
            search(
                settings(tmp_path),
                lambda req: httpx.Response(
                    status,
                    stream=BrokenStream(),
                    headers={"retry-after": "8"},
                ),
            )
        )
    if status != 200:
        assert caught.value.info.retry_after == "8"


def test_secret_query_blocked_and_success_response_scrubbed(tmp_path):
    with pytest.raises(ToolFailure, match="BOCHA_SENSITIVE_QUERY"):
        asyncio.run(
            search(
                settings(tmp_path),
                lambda req: pytest.fail("must not send"),
                SearchParameters(query="fixture-bocha-key"),
            )
        )
    response = asyncio.run(
        search(
            settings(tmp_path),
            lambda req: httpx.Response(
                200,
                json=wire(
                    item(summary="fixture-bocha-key sk-unlistedcredential123"),
                    secret="private",
                ),
            ),
        )
    )
    assert response.items[0].summary == "[REDACTED] [REDACTED]"
    assert response.raw["secret"] == "[REDACTED]"


def test_domain_condition_and_language_intent(tmp_path):
    parameters = SearchParameters(
        query="AI model release",
        language="en",
        domains=("EXAMPLE.COM.", "example.com", "模型.cn"),
        limit=10,
    )

    def handler(request):
        payload = json.loads(request.content)
        assert payload["query"] == "AI model release (site:example.com OR site:xn--xgs754b.cn)"
        assert payload["count"] == 10 and "language" not in payload
        assert payload["include"] == "example.com|xn--xgs754b.cn"
        return httpx.Response(200, json=wire())

    asyncio.run(search(settings(tmp_path), handler, parameters))


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": " "},
        {"query": "a\nsecret"},
        {"query": "AI", "limit": 0},
        {"query": "AI", "limit": 11},
        {"query": "AI", "language": "fr"},
        {"query": "AI", "domains": ("https://example.com",)},
        {"query": "AI", "domains": ("example.com/path",)},
        {"query": "AI", "domains": ("example.com:443",)},
        {"query": "AI", "domains": ("127.0.0.1",)},
        {"query": "AI", "domains": ("localhost",)},
        {"query": "AI", "domains": ("host.local",)},
        {"query": "AI", "domains": ("example.com OR evil.com",)},
        {"query": "AI", "domains": ("-evil.com",)},
        {"query": "AI", "domains": ("a" * 64 + ".com",)},
        {"query": "AI", "api_key": "evil"},
    ],
)
def test_invalid_parameters(arguments):
    with pytest.raises(ValidationError):
        SearchParameters(**arguments)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/news",
        "https://user:pass@example.com/",
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://[::1]/",
        "https://example.com:bad/",
        "https://example.com:99999/",
        "https://example.com\\evil/",
        "https://example.com/a b",
        "//example.com/a",
        "https://bad.local/",
        "https://example.com/\x00",
    ],
)
def test_unsafe_candidate_urls(url):
    with pytest.raises(ValueError):
        candidate_url(url)


def test_url_normalization_and_domain_label_boundary():
    assert candidate_url("https://EXAMPLE.com:443#section") == "https://example.com/"
    assert candidate_url("http://8.8.8.8:8080/a?q=1#x") == "http://8.8.8.8:8080/a?q=1"
    assert candidate_url("https://[2606:4700:4700::1111]/") == "https://[2606:4700:4700::1111]/"
    assert matches_domains("https://news.example.com/a", ("example.com",))
    assert not matches_domains("https://example.com.evil.com/a", ("example.com",))
    assert not matches_domains("https://notexample.com/a", ("example.com",))


def test_startup_closes_client_without_searching(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("startup must not make a network call")

    monkeypatch.setattr(BochaClient, "search", forbidden)
    app = create_app(settings(tmp_path))
    with TestClient(app) as http:
        client = app.state.bocha_client
        assert not client.http.is_closed
        assert app.state.tool_registry.get("web_search").retry_safe
        assert http.get("/health").json() == {"status": "ok"}
    assert client.http.is_closed
