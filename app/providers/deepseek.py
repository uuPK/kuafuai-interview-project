"""Non-streaming Chat Completions; each complete() performs exactly one HTTP attempt."""

import json
import math
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.agent.protocol import AssistantMessage, ModelFailure, ModelRequest, ModelResponse
from app.config import ServiceConfigurationError, Settings
from app.harness.failures import FailureDiagnostics
from app.harness.redaction import Redactor
from app.tools.models import CallId, ToolName


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)


class _Function(_WireModel):
    name: ToolName
    arguments: str = Field(max_length=120_000)


class _Call(_WireModel):
    id: CallId
    type: Literal["function"]
    function: _Function


class _Message(_WireModel):
    role: Literal["assistant"]
    content: str | None
    reasoning_content: str | None = None
    tool_calls: list[_Call] | None = Field(default=None, max_length=128)


class _Choice(_WireModel):
    index: int = Field(ge=0, le=0)
    message: _Message
    finish_reason: str = Field(min_length=1, max_length=80)


class _Envelope(_WireModel):
    id: str = Field(min_length=1, max_length=200)
    object: Literal["chat.completion"]
    model: str = Field(min_length=1)
    created: int = Field(ge=0)
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: dict[str, JsonValue]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


class DeepSeekClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        try:
            settings.require_service_config("deepseek")
        except ServiceConfigurationError:
            raise ModelFailure("DEEPSEEK_CONFIGURATION", category="configuration") from None
        self.settings = settings
        self.redactor = Redactor(settings)
        # The base path is explicit configuration: both / and /v1 are supported.
        self.url = str(settings.deepseek_base_url).rstrip("/") + "/chat/completions"
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

    def _payload(self, request: ModelRequest) -> bytes:
        request = ModelRequest.model_validate(request)
        messages = request.wire_messages()
        if self.settings.deepseek_thinking_enabled and request.tools:
            if any(
                message["role"] == "assistant"
                and not isinstance(message.get("reasoning_content"), str)
                for message in messages
            ):
                raise ModelFailure("DEEPSEEK_MISSING_REASONING", category="protocol")
        payload = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "stream": False,
            "max_tokens": self.settings.deepseek_max_tokens,
            "thinking": {
                "type": "enabled" if self.settings.deepseek_thinking_enabled else "disabled"
            },
        }
        if request.tools:
            payload.update(tools=list(request.tools), tool_choice=request.tool_choice)
        if self.settings.deepseek_thinking_enabled:
            payload["reasoning_effort"] = self.settings.deepseek_reasoning_effort
        else:
            payload["temperature"] = self.settings.deepseek_temperature
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (ValueError, RecursionError):
            raise ModelFailure("DEEPSEEK_INPUT_JSON", category="protocol") from None
        if len(encoded) > self.settings.max_model_input_chars:
            raise ModelFailure("DEEPSEEK_INPUT_TOO_LARGE", category="configuration")
        # Credentials are attached to headers only; an echoed credential cannot enter history.
        if self.redactor.contains_credential(encoded):
            raise ModelFailure("DEEPSEEK_SENSITIVE_INPUT", category="protocol")
        return encoded.encode("utf-8")

    def _request_id(self, response: httpx.Response) -> str | None:
        value = response.headers.get("x-request-id") or response.headers.get("x-ds-request-id")
        return self.redactor.clean(value)[:200] if value else None

    def _diagnostics(self, response, raw=None, *, summary=None):
        if summary is None:
            # Structured redaction runs before truncation, so partial credentials cannot leak.
            summary = (
                json.dumps(self.redactor.clean(raw), ensure_ascii=False)
                if raw is not None
                else "响应未提供可解析的 JSON 错误摘要。"
            )
        return FailureDiagnostics(
            http_status=response.status_code,
            provider_request_id=self._request_id(response),
            summary=summary[:500],
        )

    def _http_failure(self, response, raw):
        status = response.status_code
        if status in {401, 403}:
            code, category, retryable = "DEEPSEEK_AUTHENTICATION", "authentication", False
        elif status == 402:
            code, category, retryable = "DEEPSEEK_BALANCE", "quota", False
        elif status == 429 or status >= 500:
            code, category, retryable = (
                "DEEPSEEK_RATE_LIMIT" if status == 429 else "DEEPSEEK_UNAVAILABLE",
                "transient",
                True,
            )
        elif status == 404 or 300 <= status < 400:
            code, category, retryable = "DEEPSEEK_ENDPOINT", "configuration", False
        else:
            code, category, retryable = "DEEPSEEK_HTTP_PROTOCOL", "protocol", False
        retry_after = response.headers.get("retry-after")
        raise ModelFailure(
            code,
            category=category,
            retryable=retryable,
            retry_after=retry_after
            if retry_after is not None and len(retry_after) <= 200
            else None,
            diagnostics=self._diagnostics(response, raw),
        )

    def _parse(self, response, raw, *, has_tools: bool) -> ModelResponse:
        try:
            envelope = _Envelope.model_validate(raw)
            choice = envelope.choices[0]
            message = choice.message
            if (
                self.settings.deepseek_thinking_enabled
                and has_tools
                and message.reasoning_content is None
            ):
                raise ValueError("missing reasoning_content")
            normalized = {
                "role": message.role,
                "content": message.content,
                "reasoning_content": message.reasoning_content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": call.type,
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in message.tool_calls or []
                ],
            }
            assistant = AssistantMessage.model_validate_json(
                json.dumps(normalized, ensure_ascii=False)
            )
            return ModelResponse(
                message=assistant,
                response_id=envelope.id,
                provider_request_id=self._request_id(response),
                finish_reason=choice.finish_reason,
                usage=envelope.usage,
                raw=raw,
            )
        except (ValidationError, ValueError, RecursionError):
            raise ModelFailure(
                "DEEPSEEK_RESPONSE_SCHEMA",
                category="protocol",
                diagnostics=self._diagnostics(response, raw),
            ) from None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        content = self._payload(request)
        try:
            async with self.http.stream(
                "POST",
                self.url,
                content=content,
                headers={
                    "Authorization": "Bearer " + self.settings.deepseek_api_key.get_secret_value(),
                    "Content-Type": "application/json",
                },
                follow_redirects=False,
            ) as response:
                body = bytearray()
                oversized = False
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.settings.max_model_response_bytes:
                            oversized = True
                            break
                        body.extend(chunk)
                except httpx.TransportError:
                    if response.status_code != 200:
                        self._http_failure(response, None)
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
                    self._http_failure(response, raw)
                if oversized or raw is None:
                    raise ModelFailure(
                        "DEEPSEEK_RESPONSE_TOO_LARGE" if oversized else "DEEPSEEK_RESPONSE_JSON",
                        category="protocol",
                        diagnostics=self._diagnostics(
                            response,
                            summary="响应超过大小限制。"
                            if oversized
                            else "响应不是完整有效的 JSON。",
                        ),
                    )
                return self._parse(response, raw, has_tools=bool(request.tools))
        except httpx.TimeoutException:
            raise ModelFailure("DEEPSEEK_TIMEOUT", category="transient", retryable=True) from None
        except httpx.InvalidURL:
            raise ModelFailure("DEEPSEEK_ENDPOINT", category="configuration") from None
        except httpx.TransportError:
            raise ModelFailure("DEEPSEEK_NETWORK", category="transient", retryable=True) from None
