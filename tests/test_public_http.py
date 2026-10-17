import asyncio
import socket
import ssl
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from app.config import Settings
from app.providers.public_http import (
    CheckedStream,
    PublicBackend,
    PublicHttpClient,
    PublicTransport,
    public_address,
    resolve_addresses,
)
from app.tools.models import ToolFailure


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self, address="8.8.8.8", body=b"", upgraded_address=None):
        self.address, self.body, self.upgraded_address = address, body, upgraded_address
        self.writes, self.closed, self.sni = [], False, None

    async def read(self, max_bytes, timeout=None):
        value, self.body = self.body[:max_bytes], self.body[max_bytes:]
        return value

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni = server_hostname
        if self.upgraded_address:
            self.address = self.upgraded_address
        return self

    def get_extra_info(self, info):
        return (self.address, 443) if info == "server_addr" and self.address else None


async def answers(host, port):
    return ["8.8.8.8"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "2002:0808:0808::1",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        "fe80::1%eth0",
        "invalid",
        None,
    ],
)
def test_non_public_and_transition_addresses_blocked(address):
    with pytest.raises(ToolFailure, match="PUBLIC_ADDRESS_BLOCKED"):
        public_address(address)


def test_public_ip_normalization():
    assert str(public_address("8.8.8.8")) == "8.8.8.8"
    assert str(public_address("::ffff:8.8.8.8")) == "8.8.8.8"
    assert str(public_address("2606:4700:4700::1111")) == "2606:4700:4700::1111"


def test_dns_answers_are_pinned_and_peer_checked_before_write():
    calls = []
    stream = Stream()

    async def connect(host, port, **kwargs):
        calls.append((host, port))
        return stream

    async def run():
        backend = PublicBackend(resolver=answers, backend=SimpleNamespace(connect_tcp=connect))
        checked = await backend.connect_tcp("news.example.com", 443, timeout=2)
        await checked.start_tls(ssl.create_default_context(), "news.example.com", timeout=2)
        await checked.write(b"GET / HTTP/1.1\r\n")
        assert stream.sni == "news.example.com"
        assert stream.writes
        await checked.aclose()

    asyncio.run(run())
    assert calls == [("8.8.8.8", 443)] and stream.closed


@pytest.mark.parametrize(
    "values,code",
    [
        ([], "PUBLIC_DNS_INVALID"),
        (["8.8.8.8"] * 17, "PUBLIC_DNS_INVALID"),
        (["8.8.8.8", "10.0.0.1"], "PUBLIC_ADDRESS_BLOCKED"),
    ],
)
def test_invalid_dns_set_never_connects(values, code):
    async def resolve(host, port):
        return values

    async def forbidden(*args, **kwargs):
        pytest.fail("must reject before TCP connect")

    with pytest.raises(ToolFailure, match=code):
        asyncio.run(
            PublicBackend(
                resolver=resolve, backend=SimpleNamespace(connect_tcp=forbidden)
            ).connect_tcp("example.com", 443)
        )


@pytest.mark.parametrize("peer", ["10.0.0.1", "1.1.1.1", None])
def test_peer_mismatch_closes_connection_without_sending_http(peer):
    stream = Stream(address=peer)

    async def connect(*args, **kwargs):
        return stream

    with pytest.raises(ToolFailure):
        asyncio.run(
            PublicBackend(
                resolver=answers, backend=SimpleNamespace(connect_tcp=connect)
            ).connect_tcp("example.com", 443)
        )
    assert stream.closed and stream.writes == []


def test_tls_peer_is_checked_again():
    stream = Stream(upgraded_address="127.0.0.1")
    checked = CheckedStream(stream, public_address("8.8.8.8"))
    with pytest.raises(ToolFailure):
        asyncio.run(checked.start_tls(ssl.create_default_context(), "example.com"))
    assert stream.closed and stream.writes == []


@pytest.mark.parametrize("failure", [httpcore.ConnectTimeout, asyncio.CancelledError])
def test_failed_or_cancelled_tls_closes_original_stream(failure):
    class BrokenTLS(Stream):
        async def start_tls(self, *args):
            raise failure()

    stream = BrokenTLS()
    checked = CheckedStream(stream, public_address("8.8.8.8"))
    with pytest.raises(failure):
        asyncio.run(checked.start_tls(ssl.create_default_context(), "example.com"))
    assert stream.closed and not stream.writes


def test_tls_precheck_failure_closes_original_stream():
    stream = Stream(address="1.1.1.1")
    checked = CheckedStream(stream, public_address("8.8.8.8"))
    with pytest.raises(ToolFailure, match="PUBLIC_PEER_MISMATCH"):
        asyncio.run(checked.start_tls(ssl.create_default_context(), "example.com"))
    assert stream.closed and stream.sni is None


def test_whole_fetch_deadline_closes_socket_during_tls_handshake():
    class PendingTLS(Stream):
        async def start_tls(self, *args):
            await asyncio.Event().wait()

    stream = PendingTLS()

    async def connect(*args, **kwargs):
        return stream

    async def run():
        transport = PublicTransport(
            backend=PublicBackend(resolver=answers, backend=SimpleNamespace(connect_tcp=connect))
        )
        config = Settings(_env_file=None, request_timeout_seconds=0.02)
        async with PublicHttpClient(config, transport=transport) as client:
            with pytest.raises(ToolFailure, match="FETCH_TIMEOUT"):
                await client.get("https://example.com/article")
        assert stream.closed and not stream.writes

    asyncio.run(run())


def test_dns_timeout_and_fallback_connections():
    seen = []

    async def resolve(host, port):
        return ["8.8.8.8", "1.1.1.1"]

    async def connect(host, *args, **kwargs):
        seen.append(host)
        if host == "8.8.8.8":
            raise httpcore.ConnectError("unreachable")
        return Stream(address=host)

    asyncio.run(
        PublicBackend(resolver=resolve, backend=SimpleNamespace(connect_tcp=connect)).connect_tcp(
            "example.com", 443
        )
    )
    assert seen == ["8.8.8.8", "1.1.1.1"]

    async def timeout(*args):
        raise TimeoutError

    with pytest.raises(httpcore.ConnectTimeout):
        asyncio.run(PublicBackend(resolver=timeout).connect_tcp("example.com", 443))


def test_default_resolver_and_unix_socket_boundary(monkeypatch):
    async def run():
        assert await resolve_addresses("8.8.8.8", 443) == ["8.8.8.8"]
        with pytest.raises(ToolFailure):
            await resolve_addresses("127.0.0.1", 443)

        async def getaddrinfo(*args, **kwargs):
            return [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("8.8.8.8", 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
        assert await resolve_addresses("example.com", 443) == ["8.8.8.8"]
        with pytest.raises(ToolFailure):
            await PublicBackend().connect_unix_socket("/tmp/socket")
        await PublicBackend().sleep(0)

    asyncio.run(run())


def test_real_transport_bridge_keeps_host_and_sni():
    body = b"<html>public body</html>"
    stream = Stream(
        body=b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 24\r\n"
        b"Connection: close\r\n\r\n" + body
    )

    async def connect(*args, **kwargs):
        return stream

    async def run():
        config = Settings(_env_file=None)
        transport = PublicTransport(
            backend=PublicBackend(resolver=answers, backend=SimpleNamespace(connect_tcp=connect))
        )
        async with PublicHttpClient(config, transport=transport) as client:
            page = await client.get("https://news.example.com/page")
            assert page.body == body

    asyncio.run(run())
    assert stream.sni == "news.example.com"
    assert b"Host: news.example.com" in b"".join(stream.writes)


async def get(config, handler, url="https://example.com/article"):
    async with PublicHttpClient(config, transport=httpx.MockTransport(handler)) as client:
        return await client.get(url)


def test_redirects_are_validated_and_no_cookies_or_credentials_sent(tmp_path):
    seen = []
    config = Settings(_env_file=None, data_dir=tmp_path, deepseek_api_key="fixture-model-key")

    def handler(request):
        seen.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://other.example.com/new", "set-cookie": "secret=value"},
            )
        return httpx.Response(200, content=b"html", headers={"content-type": "text/html"})

    page = asyncio.run(get(config, handler))
    assert page.url == "https://other.example.com/new" and len(page.redirects) == 1
    assert len(seen) == 2


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest",
        "file:///etc/passwd",
        "https://user:pass@example.com/",
    ],
)
def test_bad_redirect_target_never_requested(location):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(ToolFailure, match="PUBLIC_URL_BLOCKED"):
        asyncio.run(get(Settings(_env_file=None), handler))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "headers,code",
    [({}, "PUBLIC_REDIRECT_INVALID"), ({"location": "/article"}, "PUBLIC_REDIRECT_LOOP")],
)
def test_redirect_missing_or_loop(headers, code):
    with pytest.raises(ToolFailure, match=code):
        asyncio.run(get(Settings(_env_file=None), lambda req: httpx.Response(302, headers=headers)))


def test_redirect_limit_and_secret_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "/next"})

    with pytest.raises(ToolFailure, match="PUBLIC_REDIRECT_LIMIT"):
        asyncio.run(get(Settings(_env_file=None, max_redirects=0), handler))
    assert len(seen) == 1
    with pytest.raises(ToolFailure, match="PUBLIC_SENSITIVE_URL"):
        asyncio.run(
            get(
                Settings(_env_file=None, deepseek_api_key="fixture-model-key"),
                lambda req: pytest.fail("must not request"),
                "https://example.com/?q=fixture-model-key",
            )
        )


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (403, "FETCH_FORBIDDEN", False),
        (404, "FETCH_HTTP_STATUS", False),
        (429, "FETCH_RATE_LIMIT", True),
        (503, "FETCH_UNAVAILABLE", True),
    ],
)
def test_http_status_feedback(status, code, retryable):
    with pytest.raises(ToolFailure, match=code) as caught:
        asyncio.run(
            get(
                Settings(_env_file=None),
                lambda req: httpx.Response(status, headers={"retry-after": "9"}),
            )
        )
    assert caught.value.info.retryable == retryable
    if retryable:
        assert caught.value.info.retry_after == "9"


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({"content-type": "application/pdf"}, b"pdf", "FETCH_CONTENT_TYPE"),
        ({"content-type": "text/html", "content-encoding": "br"}, b"", "FETCH_CONTENT_ENCODING"),
        ({"content-type": "text/html", "content-length": "20"}, b"", "FETCH_TOO_LARGE"),
        ({"content-type": "text/html"}, b"123456", "FETCH_TOO_LARGE"),
    ],
)
def test_download_type_encoding_and_size_limits(headers, body, code):
    with pytest.raises(ToolFailure, match=code):
        asyncio.run(
            get(
                Settings(_env_file=None, max_download_bytes=5),
                lambda req: httpx.Response(200, content=body, headers=headers),
            )
        )


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ReadTimeout, "FETCH_TIMEOUT"),
        (httpcore.ReadTimeout, "FETCH_TIMEOUT"),
        (httpx.ConnectError, "FETCH_NETWORK"),
        (httpcore.ConnectError, "FETCH_NETWORK"),
    ],
)
def test_network_failure_feedback(error, code):
    def handler(request):
        raise error("must not echo fixture secret")

    with pytest.raises(ToolFailure, match=code) as caught:
        asyncio.run(get(Settings(_env_file=None), handler))
    assert "fixture secret" not in caught.value.result.model_dump_json()


def test_redirect_rechecks_dns_before_second_connection():
    connections, resolved = [], []

    async def resolver(host, port):
        resolved.append(host)
        return ["8.8.8.8"] if host == "example.com" else ["10.0.0.1"]

    async def connect(host, port, **kwargs):
        connections.append(host)
        return Stream(
            body=b"HTTP/1.1 302 Found\r\nLocation: https://other.com/page\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )

    async def run():
        transport = PublicTransport(
            backend=PublicBackend(resolver=resolver, backend=SimpleNamespace(connect_tcp=connect))
        )
        async with PublicHttpClient(Settings(_env_file=None), transport=transport) as client:
            with pytest.raises(ToolFailure, match="PUBLIC_ADDRESS_BLOCKED"):
                await client.get("https://example.com/article")

    asyncio.run(run())
    assert resolved == ["example.com", "other.com"] and connections == ["8.8.8.8"]


def test_total_timeout_bounds_slow_stream_and_closes_response():
    class Slow(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self):
            self.closed = True

    stream = Slow()

    async def run():
        config = Settings(_env_file=None, request_timeout_seconds=0.03)
        with pytest.raises(ToolFailure, match="FETCH_TIMEOUT"):
            await get(
                config,
                lambda req: httpx.Response(
                    200, stream=stream, headers={"content-type": "text/html"}
                ),
            )

    asyncio.run(run())
    assert stream.closed


@pytest.mark.parametrize("cause", ["dns", "connections"])
def test_dns_failure_and_all_connections_failed_are_bounded(cause):
    async def resolver(*args):
        if cause == "dns":
            raise socket.gaierror("private details")
        return ["8.8.8.8"]

    async def connect(*args, **kwargs):
        raise httpcore.ConnectError("private details")

    with pytest.raises(httpcore.ConnectError, match="public"):
        asyncio.run(
            PublicBackend(
                resolver=resolver, backend=SimpleNamespace(connect_tcp=connect)
            ).connect_tcp("example.com", 443)
        )


def test_peer_is_rechecked_before_each_write():
    stream = Stream(address="1.1.1.1")
    with pytest.raises(ToolFailure, match="PUBLIC_PEER_MISMATCH"):
        asyncio.run(CheckedStream(stream, public_address("8.8.8.8")).write(b"secret"))
    assert stream.writes == []
