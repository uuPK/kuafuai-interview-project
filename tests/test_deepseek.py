import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.protocol import (
    AssistantMessage,
    InputMessage,
    ModelFailure,
    ModelRequest,
    ToolMessage,
)
from app.agent.runner import AgentRunner
from app.config import Settings
from app.main import create_app
from app.providers.deepseek import DeepSeekClient
from app.tools.models import ToolResult


def settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        deepseek_base_url="https://model.example.com/v1",
        deepseek_api_key="fixture-deep-key",
        deepseek_model="fixture-model",
        **overrides,
    )


def request(*messages, tools=()):
    return ModelRequest(
        messages=messages or (InputMessage(role="user", content="开始测试"),), tools=tools
    )


def wire_response(message=None, **overrides):
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "created": 1791763200,
        "model": "fixture-model",
        "choices": [
            {
                "index": 0,
                "message": message
                or {"role": "assistant", "content": "准备好了", "tool_calls": None},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
        "system_fingerprint": "fixture",
        **overrides,
    }


async def complete(config, handler, received=None):
    async with DeepSeekClient(config, transport=httpx.MockTransport(handler)) as client:
        return await client.complete(received or request())


def test_configuration_required_without_network(tmp_path):
    with pytest.raises(ModelFailure) as caught:
        DeepSeekClient(Settings(_env_file=None, data_dir=tmp_path))
    assert caught.value.info.category == "configuration"


@pytest.mark.parametrize("thinking", [False, True])
def test_request_wire_and_full_response_roundtrip(tmp_path, thinking):
    config = settings(tmp_path, deepseek_thinking_enabled=thinking, deepseek_reasoning_effort="low")
    raw = wire_response(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "受控思考",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": "not JSON",
                        "provider_extra": True,
                    },
                    "provider_extra": [1],
                }
            ],
            "provider_extra": {"saved": True},
        }
    )
    raw["choices"][0]["finish_reason"] = "tool_calls"
    schemas = (
        {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}},
    )

    def handler(received):
        assert str(received.url) == "https://model.example.com/v1/chat/completions"
        assert received.headers["authorization"] == "Bearer fixture-deep-key"
        body = json.loads(received.content)
        assert "fixture-deep-key" not in received.content.decode()
        assert body["model"] == "fixture-model" and body["stream"] is False
        assert body["tools"] == list(schemas) and body["tool_choice"] == "auto"
        assert body["max_tokens"] == config.deepseek_max_tokens
        assert body["thinking"] == {"type": "enabled" if thinking else "disabled"}
        assert (
            ("temperature" not in body and body["reasoning_effort"] == "low")
            if thinking
            else (body["temperature"] == 0.2 and "reasoning_effort" not in body)
        )
        return httpx.Response(200, json=raw, headers={"x-request-id": "provider-1"})

    response = asyncio.run(complete(config, handler, request(tools=schemas)))
    assert response.raw == raw and response.usage == raw["usage"]
    assert response.message.reasoning_content == "受控思考"
    assert response.message.tool_calls[0].function.arguments == "not JSON"
    assert response.provider_request_id == "provider-1" and response.response_id == "chat-1"
    assert type(response).model_validate_json(response.model_dump_json()) == response
    assert "受控思考" not in repr(response)


def test_historical_reasoning_sent_for_tool_and_plain_turns(tmp_path, response_factory):
    config = settings(tmp_path, deepseek_thinking_enabled=True)
    assistant = response_factory(("tool", "web_search", {}), reasoning="r1").message
    received = request(
        InputMessage(role="system", content="研究"),
        assistant,
        ToolMessage(
            tool_call_id="tool", content=ToolResult.success({"items": []}).model_dump_json()
        ),
        AssistantMessage(content="准备好了", reasoning_content="r2"),
        InputMessage(role="user", content="继续"),
        tools=({"type": "function", "function": {"name": "web_search"}},),
    )

    def handler(http_request):
        messages = json.loads(http_request.content)["messages"]
        assert messages[1]["reasoning_content"] == "r1" and messages[3]["reasoning_content"] == "r2"
        assert "tool_calls" not in messages[3] and messages[1]["content"] is None
        return httpx.Response(
            200,
            json=wire_response({"role": "assistant", "content": "完成", "reasoning_content": "r3"}),
        )

    assert asyncio.run(complete(config, handler, received)).message.reasoning_content == "r3"


def test_missing_historical_reasoning_stops_before_http(tmp_path):
    config = settings(tmp_path, deepseek_thinking_enabled=True)
    received = request(AssistantMessage(content="准备好了"), tools=({"type": "function"},))
    with pytest.raises(ModelFailure, match="MISSING_REASONING"):
        asyncio.run(complete(config, lambda req: pytest.fail("must not send"), received))


def test_empty_tool_list_uses_plain_completion_and_reasoning_is_optional(tmp_path):
    config = settings(tmp_path, deepseek_thinking_enabled=True)

    def handler(received):
        body = json.loads(received.content)
        assert "tools" not in body and "tool_choice" not in body
        return httpx.Response(200, json=wire_response())

    assert asyncio.run(complete(config, handler)).message.tool_calls == ()


@pytest.mark.parametrize(
    "status,category,retryable",
    [
        (400, "protocol", False),
        (401, "authentication", False),
        (402, "quota", False),
        (403, "authentication", False),
        (404, "configuration", False),
        (422, "protocol", False),
        (429, "transient", True),
        (500, "transient", True),
        (503, "transient", True),
    ],
)
def test_http_failures_classified_with_safe_diagnostics(tmp_path, status, category, retryable):
    config = settings(tmp_path)
    calls = []

    def handler(received):
        calls.append(True)
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "fixture-deep-key",
                    "authorization": "unknown-secret",
                    "details": {"api_key": "nested-key"},
                }
            },
            headers={"retry-after": "8", "x-request-id": "provider-1"},
        )

    with pytest.raises(ModelFailure) as caught:
        asyncio.run(complete(config, handler))
    failure = caught.value.info
    assert failure.category == category and failure.retryable is retryable
    assert failure.retry_after == "8" and calls == [True]
    assert (
        failure.diagnostics.http_status == status
        and failure.diagnostics.provider_request_id == "provider-1"
    )
    assert all(
        secret not in failure.model_dump_json()
        for secret in ["fixture-deep-key", "unknown-secret", "nested-key"]
    )


def test_redirect_never_follows_or_forwards_key(tmp_path):
    calls = []

    def handler(received):
        calls.append(str(received.url))
        return httpx.Response(307, headers={"location": "https://other.example.com/stolen"})

    with pytest.raises(ModelFailure) as caught:
        asyncio.run(complete(settings(tmp_path), handler))
    assert caught.value.info.category == "configuration" and len(calls) == 1


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout]
)
def test_transport_failure_never_echoes_exception(tmp_path, failure):
    def handler(received):
        raise failure("Authorization: fixture-deep-key", request=received)

    with pytest.raises(ModelFailure) as caught:
        asyncio.run(complete(settings(tmp_path), handler))
    assert caught.value.info.category == "transient" and caught.value.retryable
    assert "fixture-deep-key" not in str(caught.value)


@pytest.mark.parametrize(
    "change",
    [
        "choices_empty",
        "choices_many",
        "role",
        "index",
        "usage",
        "id",
        "object",
        "created",
        "content",
        "arguments",
        "function_type",
        "duplicate_id",
        "missing_reasoning",
    ],
)
def test_malformed_wire_is_protocol_failure(tmp_path, change):
    raw = wire_response(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "r",
            "tool_calls": [
                {
                    "id": "tool",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        }
    )
    choice = raw["choices"][0]
    changes = {
        "choices_empty": lambda: raw.update(choices=[]),
        "choices_many": lambda: raw["choices"].append(copy.deepcopy(choice)),
        "role": lambda: choice["message"].update(role="system"),
        "index": lambda: choice.update(index=True),
        "usage": lambda: raw.update(usage=[]),
        "id": lambda: raw.update(id=""),
        "object": lambda: raw.update(object="chat.completion.chunk"),
        "created": lambda: raw.update(created="123"),
        "content": lambda: choice["message"].update(content={"private": "fixture-deep-key"}),
        "arguments": lambda: choice["message"]["tool_calls"][0]["function"].update(arguments={}),
        "function_type": lambda: choice["message"]["tool_calls"][0].update(type="code"),
        "duplicate_id": lambda: choice["message"]["tool_calls"].append(
            copy.deepcopy(choice["message"]["tool_calls"][0])
        ),
        "missing_reasoning": lambda: choice["message"].pop("reasoning_content"),
    }
    changes[change]()
    with pytest.raises(ModelFailure) as caught:
        asyncio.run(
            complete(
                settings(tmp_path, deepseek_thinking_enabled=True),
                lambda received: httpx.Response(200, json=raw),
                request(tools=({"type": "function"},)),
            )
        )
    assert caught.value.info.category == "protocol" and not caught.value.retryable
    assert "fixture-deep-key" not in caught.value.info.diagnostics.summary


@pytest.mark.parametrize(
    "body",
    [b"not JSON", b'{"id":1,"id":2}', b'{"id":NaN}', b'{"id":1e999}', b"[]", b"null", b'{"id":'],
)
def test_invalid_json_response(tmp_path, body):
    with pytest.raises(ModelFailure) as caught:
        asyncio.run(
            complete(settings(tmp_path), lambda received: httpx.Response(200, content=body))
        )
    assert caught.value.info.category == "protocol"


def test_oversized_response_is_bounded_and_error_status_takes_priority(tmp_path):
    config = settings(tmp_path, max_model_response_bytes=256)
    for status, code in [(200, "DEEPSEEK_RESPONSE_TOO_LARGE"), (429, "DEEPSEEK_RATE_LIMIT")]:
        with pytest.raises(ModelFailure) as caught:
            asyncio.run(
                complete(
                    config,
                    lambda received, status=status: httpx.Response(status, content=b"x" * 1000),
                )
            )
        assert caught.value.code == code


@pytest.mark.parametrize("text", ["fixture-deep-key", "sk-unexpectedprovidercredential"])
def test_sensitive_input_never_sent(tmp_path, text):
    with pytest.raises(ModelFailure, match="SENSITIVE_INPUT"):
        asyncio.run(
            complete(
                settings(tmp_path),
                lambda received: pytest.fail("must not send"),
                request(InputMessage(role="user", content=text)),
            )
        )


def test_redacted_header_text_can_be_replayed(tmp_path):
    received = request(InputMessage(role="user", content="Authorization: [REDACTED]"))
    assert (
        asyncio.run(
            complete(
                settings(tmp_path), lambda req: httpx.Response(200, json=wire_response()), received
            )
        ).message.content
        == "准备好了"
    )


def test_input_size_checked_before_http(tmp_path):
    with pytest.raises(ModelFailure, match="INPUT_TOO_LARGE"):
        asyncio.run(
            complete(
                settings(tmp_path, max_model_input_chars=10),
                lambda req: pytest.fail("must not send"),
            )
        )


def test_success_metadata_and_content_are_redacted_before_save(tmp_path):
    raw = wire_response(
        {
            "role": "assistant",
            "content": "fixture-deep-key",
            "reasoning_content": "sk-unknowncredential1234",
        },
        provider={"password": "secret-password"},
    )
    response = asyncio.run(
        complete(
            settings(tmp_path),
            lambda req: httpx.Response(
                200, json=raw, headers={"x-ds-request-id": "fixture-deep-key"}
            ),
        )
    )
    assert response.message.content == response.message.reasoning_content == "[REDACTED]"
    assert response.provider_request_id == "[REDACTED]"
    assert "secret-password" not in response.model_dump_json()


def test_runner_continuous_calls_preserve_protocol_and_usage(agent_env, response_factory):
    env = agent_env
    config = settings(env.settings.data_dir, deepseek_thinking_enabled=True)
    responses = [
        response_factory(("search", "web_search", {"query": "AI"}), reasoning="r1"),
        response_factory(("fetch", "fetch_url", {"candidate_id": "fixture-model"}), reasoning="r2"),
        response_factory(
            ("submit", "submit_brief", {"draft": {"title": "中文测试稿", "items": ["固定材料"]}}),
            reasoning="r3",
        ),
    ]
    seen = []

    def handler(received):
        body = json.loads(received.content)
        index = len(seen)
        seen.append(body)
        if index:
            assert body["messages"][-1]["role"] == "tool"
            assert [
                m["reasoning_content"] for m in body["messages"] if m["role"] == "assistant"
            ] == [f"r{n + 1}" for n in range(index)]
        normalized = responses[index]
        raw = wire_response(normalized.message.model_dump(mode="json"), id=f"chat-{index}")
        raw["choices"][0]["finish_reason"] = "tool_calls"
        return httpx.Response(200, json=raw)

    async def run():
        async with DeepSeekClient(config, transport=httpx.MockTransport(handler)) as model:
            return await AgentRunner(model, env.executor, env.conversations, config).run(
                env.run.id, "user-1"
            )

    assert asyncio.run(run()).status == "completed"
    saved = env.conversations.responses(env.run.id, "user-1")
    assert len(saved) == 3 and saved[0].usage["total_tokens"] == 15
    assert saved[0].raw["choices"][0]["message"]["reasoning_content"] == "r1"


def test_runner_http_failure_diagnostics_are_durable(agent_env):
    env = agent_env

    async def run():
        async with DeepSeekClient(
            settings(env.settings.data_dir),
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    400,
                    json={"error": {"message": "invalid schema fixture-deep-key"}},
                    headers={"x-request-id": "trace-1"},
                )
            ),
        ) as model:
            return await AgentRunner(model, env.executor, env.conversations, env.settings).run(
                env.run.id, "user-1"
            )

    failed = asyncio.run(run())
    assert failed.stop_reason == "model_dependency_protocol"
    with env.database.connection() as connection:
        failure = json.loads(
            connection.execute("SELECT failure_json FROM external_requests").fetchone()[0]
        )
    assert failure["diagnostics"]["http_status"] == 400
    assert failure["diagnostics"]["provider_request_id"] == "trace-1"
    assert "fixture-deep-key" not in failure["diagnostics"]["summary"]


def test_application_creates_and_closes_configured_client(tmp_path):
    application = create_app(settings(tmp_path))
    with TestClient(application) as http:
        model = application.state.model_client
        assert isinstance(model, DeepSeekClient) and not model.http.is_closed
        assert http.get("/health").json() == {"status": "ok"}
    assert model.http.is_closed


def test_application_without_service_config_starts_offline(tmp_path):
    application = create_app(Settings(_env_file=None, data_dir=tmp_path))
    with TestClient(application):
        assert application.state.model_client is None


@pytest.mark.parametrize(
    "field,value", [("deepseek_reasoning_effort", "none"), ("max_model_response_bytes", 255)]
)
def test_invalid_provider_settings(tmp_path, field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(tmp_path, **{field: value})


@pytest.mark.parametrize(
    "status,category",
    [(401, "authentication"), (402, "quota"), (429, "transient"), (200, "transient")],
)
def test_error_status_and_retry_after_survive_body_read_failure(tmp_path, status, category):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":'
            raise httpx.ReadTimeout("fixture-deep-key")

    def handler(received):
        return httpx.Response(status, stream=BrokenStream(), headers={"retry-after": "8"})

    with pytest.raises(ModelFailure) as caught:
        asyncio.run(complete(settings(tmp_path), handler))
    assert caught.value.info.category == category
    if status != 200:
        assert caught.value.info.retry_after == "8"


def test_non_finite_input_schema_is_safe_protocol_failure(tmp_path):
    received = request(tools=({"value": float("nan")},))
    with pytest.raises(ModelFailure, match="INPUT_JSON"):
        asyncio.run(
            complete(settings(tmp_path), lambda received: pytest.fail("must not send"), received)
        )


def test_finite_float_wire_metadata_is_preserved(tmp_path):
    raw = wire_response(extra={"value": 0.25})
    assert (
        asyncio.run(complete(settings(tmp_path), lambda req: httpx.Response(200, json=raw))).raw
        == raw
    )
