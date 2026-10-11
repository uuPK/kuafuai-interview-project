"""Tool request and result contracts, independent of provider wire formats."""

import hashlib
import json
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

ToolName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")]
CallId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^\S+$")]


class ToolParameters(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class ToolInvocation(ToolParameters):
    tool_call_id: CallId
    name: ToolName
    arguments: str = Field(max_length=120_000, repr=False)

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps([self.name, self.arguments], ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ArgumentIssue(ToolParameters):
    field: str = Field(max_length=80)
    issue: str = Field(max_length=80)


class ToolError(ToolParameters):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    message: str = Field(min_length=1, max_length=400)
    retryable: bool = False
    details: tuple[ArgumentIssue, ...] = Field(default=(), max_length=10)


class ToolResult(ToolParameters):
    ok: bool
    data: JsonValue = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.ok == (self.error is not None) or (not self.ok and self.data is not None):
            raise ValueError("success requires no error; failure requires only an error")
        return self

    @classmethod
    def success(cls, data: JsonValue = None) -> Self:
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: tuple[ArgumentIssue, ...] = (),
    ) -> Self:
        return cls(
            ok=False,
            error=ToolError(code=code, message=message, retryable=retryable, details=details),
        )


class ToolFailure(Exception):
    """Handlers raise controlled failures; arbitrary exceptions are never echoed."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.result = ToolResult.failure(code, message, retryable=retryable)
        super().__init__(code)
