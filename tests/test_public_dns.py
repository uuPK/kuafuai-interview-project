import asyncio

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.providers.public_http import HTTPSResolver, PublicBackend, PublicHttpClient
from app.tools.models import ToolFailure


def answer(request, addresses=None, **changes):
    kind = int(request.url.params["type"])
    values = (
        addresses
        if addresses is not None
        else (["8.8.8.8"] if kind == 1 else ["2606:4700:4700::1111"])
    )
    return {
        "Status": 0,
        "TC": False,
        "Question": [{"name": request.url.params["name"] + ".", "type": kind}],
        "Answer": [{"type": kind, "data": value} for value in values],
        **changes,
    }


def resolver(handler):
    return HTTPSResolver(1, transport=httpx.MockTransport(handler))


def test_fixed_https_dns_queries_both_families_without_credentials_or_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    seen = []

    def respond(request):
        seen.append(request)
        assert str(request.url).startswith("https://dns.google/resolve?")
        assert set(request.url.params) == {"name", "type", "edns_client_subnet"}
        assert request.url.params["edns_client_subnet"] == "0.0.0.0/0"
        assert not {"authorization", "cookie", "proxy-authorization"} & set(request.headers)
        return httpx.Response(200, json=answer(request), headers={"set-cookie": "unexpected=1"})

    assert asyncio.run(resolver(respond)("News.Example.com", 443)) == [
        "8.8.8.8",
        "2606:4700:4700::1111",
    ]
    assert {request.url.params["type"] for request in seen} == {"1", "28"}


@pytest.mark.parametrize(
    "address", ["198.18.0.79", "127.0.0.1", "10.1.1.1", "::1", "::ffff:127.0.0.1"]
)
def test_all_dns_answers_still_require_public_addresses(address):
    def respond(request):
        kind = int(request.url.params["type"])
        values = ["8.8.8.8", address] if kind == 1 and ":" not in address else []
        if kind == 28 and ":" in address:
            values = [address]
        return httpx.Response(200, json=answer(request, values))

    with pytest.raises(ToolFailure, match="PUBLIC_ADDRESS_BLOCKED"):
        asyncio.run(resolver(respond)("news.example.com", 443))


@pytest.mark.parametrize(
    "changes",
    [
        {"Status": 2},
        {"Status": False},
        {"TC": True},
        {"Question": [{"name": "other.example.com.", "type": 1}]},
        {"Answer": "bad"},
        {"Answer": [None]},
        {"Answer": [{"type": 1, "data": "bad"}]},
        {"Answer": [{"type": 1, "data": "2606:4700:4700::1111"}]},
        {"Answer": [{"type": True, "data": "8.8.8.8"}]},
        {"Answer": [{"type": 5, "data": "cname.example.com"}] * 65},
        {"Answer": [{"type": 1, "data": f"8.8.8.{n}"} for n in range(1, 18)]},
    ],
)
def test_malformed_truncated_or_unrelated_dns_response_is_rejected(changes):
    with pytest.raises(ToolFailure, match="PUBLIC_DNS_INVALID"):
        asyncio.run(
            resolver(lambda r: httpx.Response(200, json=answer(r, **changes)))(
                "news.example.com", 443
            )
        )


@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
            "PUBLIC_DNS_UNAVAILABLE",
        ),
        (httpx.Response(503), "PUBLIC_DNS_UNAVAILABLE"),
        (httpx.Response(200, text="not JSON"), "PUBLIC_DNS_INVALID"),
        (
            httpx.Response(
                200, content=b"x" * 65_537, headers={"content-type": "application/json"}
            ),
            "PUBLIC_DNS_INVALID",
        ),
        (httpx.Response(200, json=[]), "PUBLIC_DNS_INVALID"),
    ],
)
def test_dns_http_boundaries(response, code):
    with pytest.raises(ToolFailure, match=code):
        asyncio.run(resolver(lambda r: response)("news.example.com", 443))


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ConnectTimeout("private diagnostic"), "PUBLIC_DNS_TIMEOUT"),
        (httpx.ConnectError("private diagnostic"), "PUBLIC_DNS_UNAVAILABLE"),
    ],
)
def test_dns_transport_failures_are_safe_and_retryable(error, code):
    def fail(request):
        raise error

    with pytest.raises(ToolFailure, match=code) as caught:
        asyncio.run(resolver(fail)("news.example.com", 443))
    assert caught.value.result.error.retryable
    assert "private diagnostic" not in caught.value.result.model_dump_json()


def test_dns_uses_overall_deadline_and_never_falls_back_to_system():
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=answer(request))

    with pytest.raises(ToolFailure, match="PUBLIC_DNS_TIMEOUT"):
        asyncio.run(
            HTTPSResolver(0.01, transport=httpx.MockTransport(slow))("news.example.com", 443)
        )


def test_empty_or_nxdomain_dns_is_not_valid_evidence():
    for status in [0, 3]:
        with pytest.raises(ToolFailure, match="PUBLIC_DNS_INVALID"):
            asyncio.run(
                resolver(
                    lambda r, status=status: httpx.Response(200, json=answer(r, [], Status=status))
                )("news.example.com", 443)
            )


def test_ip_literals_and_private_names_never_reach_https_dns():
    def forbidden(request):
        pytest.fail("must reject before DNS request")

    resolved = resolver(forbidden)
    assert asyncio.run(resolved("8.8.8.8", 443)) == ["8.8.8.8"]
    for host in ["127.0.0.1", "198.18.0.79", "localhost", "foo.internal", "bad..com"]:
        with pytest.raises(ToolFailure):
            asyncio.run(resolved(host, 443))


def test_resolver_configuration_is_explicit_and_preserves_network_guards():
    assert Settings(_env_file=None).public_dns == "system"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_dns="https://arbitrary.example.com")

    async def scenario():
        for mode in ["system", "google_doh"]:
            async with PublicHttpClient(Settings(_env_file=None, public_dns=mode)) as client:
                backend = client.http._transport.pool._network_backend
                assert isinstance(backend, PublicBackend)
                assert isinstance(backend.resolver, HTTPSResolver) == (mode == "google_doh")

    asyncio.run(scenario())
