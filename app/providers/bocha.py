"""Bocha REST search, one HTTP attempt; retry and budget belong to the harness."""

import json
import math
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import ServiceConfigurationError, Settings
from app.harness.failures import FailureDiagnostics
from app.harness.redaction import Redactor
from app.tools.discovery import CandidateMaterial, SearchParameters
from app.tools.models import ToolFailure


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)


class _Pages(_WireModel):
    value: list[dict]
    totalEstimatedMatches: int | None = Field(default=None, ge=0)


class _Data(_WireModel):
    webPages: _Pages | None


class _Envelope(_WireModel):
    code: int
    log_id: str | None = None
    data: _Data


@dataclass(frozen=True)
class SearchResponse:
    items: tuple[CandidateMaterial, ...]
    received_count: int
    invalid_count: int
    provider_request_id: str | None
    raw: dict = field(repr=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


class BochaClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        try:
            settings.require_service_config("bocha")
        except ServiceConfigurationError:
            raise ToolFailure(
                "BOCHA_CONFIGURATION", "博查服务配置缺失。", category="configuration"
            ) from None
        self.settings = settings
        self.redactor = Redactor(settings)
        self.http = httpx.AsyncClient(
            transport=transport,
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

    def _request_id(self, response, raw):
        value = response.headers.get("x-request-id")
        if not value and isinstance(raw, dict):
            value = raw.get("log_id")
        return self.redactor.clean(value)[:200] if isinstance(value, str) else None

    def _diagnostics(self, response, raw, *, summary=None):
        if summary is None:
            summary = (
                json.dumps(self.redactor.clean(raw), ensure_ascii=False)
                if raw is not None
                else "响应未提供有效 JSON 摘要。"
            )
        return FailureDiagnostics(
            http_status=response.status_code,
            provider_request_id=self._request_id(response, raw),
            summary=summary[:500],
        )

    def _failure(self, response, raw, status):
        if status == 401:
            code, category, message = "BOCHA_AUTHENTICATION", "authentication", "博查鉴权失败。"
        elif status in {402, 403}:
            code, category, message = "BOCHA_QUOTA", "quota", "博查余额、套餐额度或访问权限不足。"
        elif status == 429:
            code, category, message = "BOCHA_RATE_LIMIT", "transient", "博查请求限流。"
        elif 500 <= status <= 599:
            code, category, message = "BOCHA_UNAVAILABLE", "transient", "博查服务暂不可用。"
        elif status == 404 or 300 <= status < 400:
            code, category, message = (
                "BOCHA_ENDPOINT",
                "configuration",
                "博查端点或重定向配置异常。",
            )
        else:
            code, category, message = "BOCHA_HTTP_PROTOCOL", "protocol", "博查请求或响应协议异常。"
        retry_after = response.headers.get("retry-after")
        raise ToolFailure(
            code,
            message,
            category=category,
            retryable=category == "transient",
            retry_after=retry_after
            if retry_after is not None and len(retry_after) <= 200
            else None,
            diagnostics=self._diagnostics(response, raw),
        )

    def _protocol_failure(self, response, raw, code, summary):
        raise ToolFailure(
            code,
            summary,
            category="protocol",
            diagnostics=self._diagnostics(response, raw, summary=summary),
        )

    def _parse(self, response, raw):
        # HTTP 200 alone is not sufficient; Bocha also uses an application status code.
        if isinstance(raw, dict) and type(raw.get("code")) is int and raw["code"] != 200:
            self._failure(response, raw, raw["code"])
        try:
            envelope = _Envelope.model_validate(raw)
            pages = envelope.data.webPages
            rows = pages.value if pages is not None else []
        except ValidationError:
            self._protocol_failure(
                response, raw, "BOCHA_RESPONSE_SCHEMA", "博查响应结构不完整，不能视作有效零结果。"
            )
        items = []
        invalid = 0
        for row in rows:
            try:
                title = row.get("name")
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("missing title")
                dates = {
                    key: row[key] for key in ("datePublished", "dateLastCrawled") if key in row
                }
                items.append(
                    CandidateMaterial(
                        title=title.strip(),
                        url=row.get("url"),
                        snippet=row.get("snippet"),
                        summary=row.get("summary"),
                        site_name=row.get("siteName"),
                        language=row.get("language"),
                        date_clues=dates,
                    )
                )
            except (ValueError, UnicodeError):
                invalid += 1
        if rows and not items:
            self._protocol_failure(
                response, raw, "BOCHA_INVALID_RESULTS", "博查返回的条目均缺少合法标题或链接等字段。"
            )
        return SearchResponse(
            items=tuple(items),
            received_count=len(rows),
            invalid_count=invalid,
            provider_request_id=self._request_id(response, raw),
            raw=raw,
        )

    async def search(self, parameters: SearchParameters) -> SearchResponse:
        parameters = SearchParameters.model_validate(parameters)
        payload = {
            "query": parameters.provider_query,
            "freshness": "oneDay",
            "summary": True,
            "count": parameters.limit,
        }
        if parameters.domains:
            payload["include"] = "|".join(parameters.domains)
        if self.redactor.contains_credential(payload["query"]):
            raise ToolFailure("BOCHA_SENSITIVE_QUERY", "搜索词含敏感凭据，已阻止发送。")
        try:
            async with self.http.stream(
                "POST",
                str(self.settings.bocha_search_url),
                json=payload,
                headers={
                    "Authorization": "Bearer " + self.settings.bocha_api_key.get_secret_value()
                },
                follow_redirects=False,
            ) as response:
                body, oversized = bytearray(), False
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.settings.max_download_bytes:
                            oversized = True
                            break
                        body.extend(chunk)
                except httpx.TransportError:
                    if response.status_code != 200:
                        self._failure(response, None, response.status_code)
                    raise
                raw = None
                if not oversized:
                    try:
                        raw = self.redactor.clean(
                            json.loads(
                                body,
                                object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant,
                                parse_float=_finite_float,
                            )
                        )
                    except (ValueError, RecursionError):
                        pass
                if response.status_code != 200:
                    self._failure(response, raw, response.status_code)
                if oversized or raw is None:
                    self._protocol_failure(
                        response,
                        raw,
                        "BOCHA_RESPONSE_TOO_LARGE" if oversized else "BOCHA_RESPONSE_JSON",
                        "博查响应超过大小限制。" if oversized else "博查响应不是完整有效 JSON。",
                    )
                return self._parse(response, raw)
        except httpx.TimeoutException:
            raise ToolFailure("BOCHA_TIMEOUT", "博查请求超时。", retryable=True) from None
        except httpx.InvalidURL:
            raise ToolFailure(
                "BOCHA_ENDPOINT", "博查端点非法。", category="configuration"
            ) from None
        except httpx.TransportError:
            raise ToolFailure("BOCHA_NETWORK", "博查网络暂不可用。", retryable=True) from None
