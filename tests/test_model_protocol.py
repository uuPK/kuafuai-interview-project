import asyncio

import pytest
from pydantic import ValidationError

from app.agent.protocol import (
    AssistantMessage,
    FunctionCall,
    InputMessage,
    ModelFailure,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ToolMessage,
)
from app.agent.stubs import ScriptedModel


def request(messages):
    return ModelRequest(messages=tuple(messages), tools=())


def test_full_response_round_trip_preserves_reasoning_and_raw_fields(response_factory) -> None:
    response = response_factory(
        ("lookup", "web_search", "not valid JSON"),
        content="开始研究。",
        reasoning="仅供协议回传的字段",
        response_id="provider-1",
        usage={"prompt_tokens": 9, "completion_tokens": 4},
        raw={"provider_extra": {"trace": [1, None, "原始值"]}},
    )
    assert ModelResponse.model_validate_json(response.model_dump_json()) == response
    assert response.message.tool_calls[0].invocation().arguments == "not valid JSON"
    assert "仅供协议" not in repr(response)
    assert "原始值" not in repr(response)


def test_wire_messages_keep_reasoning_in_tool_and_text_turns(response_factory) -> None:
    tool_turn = response_factory(("search", "web_search", {}), reasoning="r1").message
    text_turn = AssistantMessage(content="稿件准备中", reasoning_content="r2")
    messages = request(
        [
            InputMessage(role="system", content="研究"),
            tool_turn,
            ToolMessage(tool_call_id="search", content='{"ok":true}'),
            text_turn,
            InputMessage(role="user", content="请提交"),
        ]
    ).wire_messages()
    assert messages[1]["reasoning_content"] == "r1"
    assert messages[1]["content"] is None
    assert messages[3]["reasoning_content"] == "r2"
    assert messages[1]["tool_calls"][0]["id"] == "search"
    assert "tool_calls" not in messages[3]
    assert messages[2] == {"role": "tool", "tool_call_id": "search", "content": '{"ok":true}'}


@pytest.mark.parametrize("kind", ["missing", "orphan", "wrong_id", "out_of_order", "interrupted"])
def test_requests_reject_incomplete_or_incorrect_feedback(response_factory, kind) -> None:
    assistant = response_factory(("first", "web_search", {}), ("second", "fetch_url", {})).message
    messages = {
        "missing": [assistant],
        "orphan": [ToolMessage(tool_call_id="first", content="{}")],
        "wrong_id": [assistant, ToolMessage(tool_call_id="other", content="{}")],
        "out_of_order": [assistant, ToolMessage(tool_call_id="second", content="{}")],
        "interrupted": [assistant, InputMessage(role="user", content="继续")],
    }
    with pytest.raises(ValidationError):
        request(messages[kind])


def test_duplicate_call_ids_are_rejected_within_and_across_turns(response_factory) -> None:
    with pytest.raises(ValidationError):
        response_factory(("same", "web_search", {}), ("same", "fetch_url", {}))
    assistant = response_factory(("same", "web_search", {})).message
    feedback = ToolMessage(tool_call_id="same", content="{}")
    with pytest.raises(ValidationError):
        request([assistant, feedback, assistant, feedback])


def test_protocol_rejects_unknown_fields_and_bad_call_types() -> None:
    with pytest.raises(ValidationError):
        ModelToolCall(id="call", type="code", function=FunctionCall(name="sample", arguments="{}"))
    with pytest.raises(ValidationError):
        ModelResponse(message=AssistantMessage(), password="secret")
    with pytest.raises(ValidationError):
        ModelRequest(messages=(), tools=(), tool_choice="required")


def test_scripted_model_copies_responses_and_records_requests(response_factory) -> None:
    response = response_factory(content="普通文本", raw={"nested": ["original"]})
    model = ScriptedModel([lambda received: response])
    original = request([InputMessage(role="user", content="任务")])
    returned = asyncio.run(model.complete(original))
    returned.raw["nested"].append("changed")
    assert response.raw == {"nested": ["original"]}
    assert model.requests == [original]
    with pytest.raises(ModelFailure, match="FIXTURE_EXHAUSTED"):
        asyncio.run(model.complete(original))
