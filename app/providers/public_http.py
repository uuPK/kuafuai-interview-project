"""Credential-free public GETs with pinned DNS, checked peers, and bounded redirects."""

import asyncio
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin

import httpcore
import httpx

from app.config import Settings
from app.harness.redaction import Redactor
from app.tools.discovery import candidate_url, domain_name
from app.tools.models import ToolFailure


def public_address(value):
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError
        if isinstance(address, ipaddress.IPv6Address) and (address.sixtofour or address.teredo):
            raise ValueError
        return address
    except ValueError:
        raise ToolFailure(
            "PUBLIC_ADDRESS_BLOCKED", "目标解析或连接地址不属于允许的公网范围。"
        ) from None


async def resolve_addresses(host, port):
    try:
        return [str(public_address(host))]
    except ToolFailure:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(record[4][0] for record in records))


class HTTPSResolver:
    """Deployment-selected DNS only; returned IPs still pass the public/peer guards."""

    def __init__(self, timeout, *, transport=None):
        self.timeout, self.transport = timeout, transport

    async def __call__(self, host, port):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            try:
                host = domain_name(host)
            except (ValueError, UnicodeError):
                raise ToolFailure("PUBLIC_DNS_INVALID", "目标域名不符合公网解析要求。") from None
        else:
            return [str(public_address(host))]
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.timeout,
                    transport=self.transport,
                ) as client:
                    groups = await asyncio.gather(
                        self._query(client, host, 1),
                        self._query(client, host, 28),
                        return_exceptions=True,
                    )
                    for group in groups:
                        if isinstance(group, BaseException):
                            raise group
        except (TimeoutError, httpx.TimeoutException):
            raise ToolFailure(
                "PUBLIC_DNS_TIMEOUT", "公网域名解析超时，请检查网络后重试。", retryable=True
            ) from None
        except httpx.TransportError:
            raise ToolFailure(
                "PUBLIC_DNS_UNAVAILABLE", "公网域名解析服务暂不可用。", retryable=True
            ) from None
        addresses = list(dict.fromkeys(value for group in groups for value in group))
        if not addresses or len(addresses) > 16:
            raise ToolFailure("PUBLIC_DNS_INVALID", "目标未提供有效且有界的公网解析结果。")
        return [str(public_address(value)) for value in addresses]

    async def _query(self, client, host, kind):
        # Fixed trusted endpoint, no credentials/cookies, no redirect or OS proxy.
        request = httpx.Request(
            "GET",
            "https://dns.google/resolve",
            params={"name": host, "type": kind, "edns_client_subnet": "0.0.0.0/0"},
            headers={"Accept": "application/dns-json", "Accept-Encoding": "identity"},
            extensions={
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), self.timeout)
            },
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code != 200:
                raise ToolFailure(
                    "PUBLIC_DNS_UNAVAILABLE", "公网域名解析服务暂不可用。", retryable=True
                )
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in {"application/json", "application/dns-json"} or (
                response.headers.get("content-encoding", "identity").lower() != "identity"
            ):
                raise ValueError
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 65_536:
                    raise ValueError
                body.extend(chunk)
            data = json.loads(body)
            if (
                not isinstance(data, dict)
                or type(data.get("Status")) is not int
                or data["Status"] not in {0, 3}
                or data.get("TC") is not False
                or data.get("Question") != [{"name": host + ".", "type": kind}]
            ):
                raise ValueError
            records = data.get("Answer", [])
            if not isinstance(records, list) or len(records) > 64:
                raise ValueError
            values = []
            for record in records:
                if not isinstance(record, dict) or type(record.get("type")) is not int:
                    raise ValueError
                if record["type"] in {1, 28}:
                    value = record.get("data")
                    if ipaddress.ip_address(value).version != (4 if record["type"] == 1 else 6):
                        raise ValueError
                    values.append(str(public_address(value)))
            return values
        except (ValueError, TypeError, KeyError):
            raise ToolFailure("PUBLIC_DNS_INVALID", "公网域名解析响应无效，未连接目标。") from None
        finally:
            await response.aclose()


class CheckedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, address):
        self.stream, self.address = stream, address

    def check_peer(self):
        peer = self.stream.get_extra_info("server_addr")
        if not isinstance(peer, tuple) or len(peer) < 2 or public_address(peer[0]) != self.address:
            raise ToolFailure("PUBLIC_PEER_MISMATCH", "实际连接地址与已校验地址不一致。")

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout=timeout)

    async def write(self, buffer, timeout=None):
        self.check_peer()
        await self.stream.write(buffer, timeout=timeout)

    async def aclose(self):
        await self.stream.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        upgraded = self.stream
        try:
            self.check_peer()
            upgraded = await self.stream.start_tls(ssl_context, server_hostname, timeout)
            checked = CheckedStream(upgraded, self.address)
            checked.check_peer()
            return checked
        except BaseException:
            # TLS may be cancelled before httpcore owns an HTTP connection to close.
            await upgraded.aclose()
            raise

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)


class PublicBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, resolver=resolve_addresses, backend=None):
        self.resolver = resolver
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            async with asyncio.timeout(timeout):
                answers = await self.resolver(host, port)
                if not answers or len(answers) > 16:
                    raise ToolFailure("PUBLIC_DNS_INVALID", "目标未提供有效且有界的公网解析结果。")
                # Reject the entire answer set, rather than selecting around a private address.
                addresses = tuple(dict.fromkeys(public_address(answer) for answer in answers))
                for address in addresses:
                    try:
                        stream = await self.backend.connect_tcp(
                            str(address),
                            port,
                            timeout=timeout,
                            local_address=local_address,
                            socket_options=socket_options,
                        )
                    except httpcore.ConnectError:
                        continue
                    checked = CheckedStream(stream, address)
                    try:
                        checked.check_peer()
                    except BaseException:
                        await stream.aclose()
                        raise
                    return checked
                raise httpcore.ConnectError("public connection failed")
        except (TimeoutError, socket.gaierror) as error:
            exception = (
                httpcore.ConnectTimeout
                if isinstance(error, TimeoutError)
                else httpcore.ConnectError
            )
            raise exception("public DNS/connection failed") from None

    async def connect_unix_socket(self, *args, **kwargs):
        raise ToolFailure("PUBLIC_ADDRESS_BLOCKED", "网页抓取不允许 Unix socket。")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class CoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, backend=None):
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            network_backend=backend or PublicBackend(),
            max_keepalive_connections=0,
            retries=0,
        )

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


@dataclass(frozen=True)
class PublicPage:
    url: str
    body: bytes
    redirects: tuple[str, ...]


class PublicHttpClient:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings, self.redactor = settings, Redactor(settings)
        self.http = httpx.AsyncClient(
            transport=transport
            or PublicTransport(
                backend=PublicBackend(
                    resolver=HTTPSResolver(settings.request_timeout_seconds)
                    if settings.public_dns == "google_doh"
                    else resolve_addresses
                )
            ),
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        await self.http.aclose()

    async def get(self, url: str, *, accept="text/html, application/xhtml+xml", html_only=True):
        """One bounded GET attempt; a redirect is another checked hop within that attempt."""
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                return await self._get(url, accept=accept, html_only=html_only)
        except TimeoutError:
            raise ToolFailure("FETCH_TIMEOUT", "网页请求超时。", retryable=True) from None

    async def _get(self, url, *, accept, html_only):
        try:
            visited = []
            while True:
                try:
                    url = candidate_url(url)
                except (ValueError, UnicodeError):
                    raise ToolFailure(
                        "PUBLIC_URL_BLOCKED", "网页地址不符合公开 HTTP(S) 地址要求。"
                    ) from None
                if self.redactor.contains_credential(url):
                    raise ToolFailure("PUBLIC_SENSITIVE_URL", "网页地址含敏感凭据，已阻止访问。")
                if url in visited:
                    raise ToolFailure("PUBLIC_REDIRECT_LOOP", "网页发生循环跳转，请换来源。")
                if len(visited) > self.settings.max_redirects:
                    raise ToolFailure("PUBLIC_REDIRECT_LIMIT", "网页跳转次数超过限制，请换来源。")
                visited.append(url)
                # Build directly so a prior response's Cookie jar cannot affect later GETs.
                request = httpx.Request(
                    "GET",
                    url,
                    headers={
                        "Accept": accept,
                        "Accept-Encoding": "identity",
                        "User-Agent": "DailyAINews/0.1 (+public source research)",
                    },
                    extensions={
                        "timeout": dict.fromkeys(
                            ("connect", "read", "write", "pool"),
                            self.settings.request_timeout_seconds,
                        )
                    },
                )
                response = await self.http.send(request, stream=True, follow_redirects=False)
                try:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolFailure("PUBLIC_REDIRECT_INVALID", "网页跳转缺少目标地址。")
                        url = urljoin(url, location)
                        continue
                    if status == 429 or 500 <= status <= 599:
                        retry_after = response.headers.get("retry-after")
                        raise ToolFailure(
                            "FETCH_RATE_LIMIT" if status == 429 else "FETCH_UNAVAILABLE",
                            "网页限流或暂不可用。",
                            retryable=True,
                            retry_after=retry_after
                            if retry_after and len(retry_after) <= 200
                            else None,
                        )
                    if status != 200:
                        raise ToolFailure(
                            "FETCH_FORBIDDEN" if status in {401, 403} else "FETCH_HTTP_STATUS",
                            f"网页返回 HTTP {status}，可选择其他来源。",
                        )
                    content_type = (
                        response.headers.get("content-type", "").split(";")[0].lower().strip()
                    )
                    if html_only and content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ToolFailure("FETCH_CONTENT_TYPE", "网页未返回支持的 HTML 内容。")
                    if response.headers.get("content-encoding", "identity").lower() not in {
                        "",
                        "identity",
                    }:
                        raise ToolFailure(
                            "FETCH_CONTENT_ENCODING", "网页未遵循 identity 编码，无法保证下载边界。"
                        )
                    length = response.headers.get("content-length")
                    if (
                        length
                        and length.isdigit()
                        and int(length) > self.settings.max_download_bytes
                    ):
                        raise ToolFailure("FETCH_TOO_LARGE", "网页下载大小超过限制。")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.settings.max_download_bytes:
                            raise ToolFailure("FETCH_TOO_LARGE", "网页下载大小超过限制。")
                        body.extend(chunk)
                    return PublicPage(url=url, body=bytes(body), redirects=tuple(visited[:-1]))
                finally:
                    await response.aclose()
        except (httpx.TimeoutException, httpcore.TimeoutException):
            raise ToolFailure("FETCH_TIMEOUT", "网页请求超时。", retryable=True) from None
        except (httpx.TransportError, httpcore.NetworkError, httpcore.ProtocolError):
            raise ToolFailure("FETCH_NETWORK", "网页连接暂不可用。", retryable=True) from None
