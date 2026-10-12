"""Validated messages; keep reasoning and raw responses in controlled storage only."""

from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from app.harness.failures import FailureDiagnostics, FailureInfo
from app.tools.models import CallId, ToolInvocation, ToolName, ToolParameters


class FunctionCall(ToolParameters):
    name: ToolName
    arguments: str = Field(max_length=120_000, repr=False)


class ModelToolCall(ToolParameters):
    id: CallId
    type: Literal["function"] = "function"
    function: FunctionCall

    def invocation(self) -> ToolInvocation:
        return ToolInvocation(
            tool_call_id=self.id, name=self.function.name, arguments=self.function.arguments
        )


class InputMessage(ToolParameters):
    role: Literal["system", "user"]
    content: str = Field(repr=False)


class AssistantMessage(ToolParameters):
    role: Literal["assistant"] = "assistant"
    content: str | None = Field(default=None, repr=False)
    reasoning_content: str | None = Field(default=None, repr=False)
    tool_calls: tuple[ModelToolCall, ...] = ()

    @model_validator(mode="after")
    def unique_calls(self) -> Self:
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("assistant tool call IDs must be unique")
        return self


class ToolMessage(ToolParameters):
    role: Literal["tool"] = "tool"
    tool_call_id: CallId
    content: str = Field(repr=False)


Message = Annotated[InputMessage | AssistantMessage | ToolMessage, Field(discriminator="role")]
MESSAGE_ADAPTER = TypeAdapter(Message)


class ModelResponse(ToolParameters):
    message: AssistantMessage
    response_id: str | None = Field(default=None, max_length=200, repr=False)
    provider_request_id: str | None = Field(default=None, max_length=200, repr=False)
    finish_reason: str = Field(default="stop", max_length=80)
    usage: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    raw: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class ModelRequest(ToolParameters):
    messages: tuple[Message, ...]
    tools: tuple[dict[str, JsonValue], ...]
    tool_choice: Literal["auto"] = "auto"

    @model_validator(mode="after")
    def complete_pairing(self) -> Self:
        pending: list[str] = []
        seen: set[str] = set()
        for message in self.messages:
            if isinstance(message, ToolMessage):
                if not pending or message.tool_call_id != pending.pop(0):
                    raise ValueError("tool feedback must match the assistant call order")
            else:
                if pending:
                    raise ValueError("assistant calls require feedback before the next message")
                if isinstance(message, AssistantMessage):
                    pending = [call.id for call in message.tool_calls]
                    if seen.intersection(pending):
                        raise ValueError("tool call IDs cannot be reused across responses")
                    seen.update(pending)
        if pending:
            raise ValueError("model request cannot contain unresolved tool calls")
        return self

    def wire_messages(self) -> list[dict]:
        """Build provider messages without dropping any historical reasoning field."""
        messages = []
        for message in self.messages:
            value = message.model_dump(mode="json", exclude_none=True)
            if isinstance(message, AssistantMessage):
                value["content"] = message.content
                if not message.tool_calls:
                    value.pop("tool_calls")
            messages.append(value)
        return messages


class ModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ModelFailure(Exception):
    """Controlled dependency failure; exception text is never copied into events."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: str | None = None,
        category: str | None = None,
        diagnostics: FailureDiagnostics | None = None,
    ) -> None:
        self.info = FailureInfo(
            code=code,
            retryable=retryable,
            retry_after=retry_after,
            category=category or ("transient" if retryable else "observation"),
            diagnostics=diagnostics,
        )
        self.code = self.info.code
        self.retryable = self.info.retryable
        super().__init__(code)
