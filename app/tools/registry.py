"""Trusted tool registration; expose only argument schemas to the model."""

import inspect
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from app.tools.context import ToolContext
from app.tools.models import ToolName, ToolParameters, ToolResult

RESERVED_FIELDS = {
    "user_id",
    "run_id",
    "tool_call_id",
    "recipient",
    "working_dir",
    "settings",
    "api_key",
}
Handler = Callable[[ToolContext, ToolParameters], Awaitable[ToolResult]]
CompletionStatus = Literal["completed", "no_news"]
CompletionResolver = Callable[[ToolResult], CompletionStatus | None]
ResultHook = Callable[[sqlite3.Connection, object], None]


class ToolMetadata(ToolParameters):
    name: ToolName
    description: str = Field(min_length=1, max_length=2000)
    budget_dimension: Literal["tool_calls", "searches", "fetches"] = "tool_calls"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: type[ToolParameters]
    handler: Handler
    budget_dimension: Literal["tool_calls", "searches", "fetches"] = "tool_calls"
    completion_status: CompletionResolver | None = None
    retry_safe: bool = False
    on_completed: ResultHook | None = None

    def __post_init__(self) -> None:
        ToolMetadata(
            name=self.name, description=self.description, budget_dimension=self.budget_dimension
        )
        if not issubclass(self.parameters, ToolParameters):
            raise ValueError("tool parameters must inherit ToolParameters")
        if self.parameters.model_config.get("extra") != "forbid":
            raise ValueError("tool parameters must reject unknown fields")
        if not self.parameters.model_config.get("strict"):
            raise ValueError("tool parameters must use strict validation")
        exposed_fields = set(self.parameters.model_fields) | set(
            self.parameters.model_json_schema()["properties"]
        )
        if RESERVED_FIELDS.intersection(exposed_fields):
            raise ValueError("tool parameters cannot contain server-bound fields")
        if not inspect.iscoroutinefunction(self.handler):
            raise ValueError("tool handler must be asynchronous")
        if self.completion_status is not None and (
            not callable(self.completion_status)
            or inspect.iscoroutinefunction(self.completion_status)
        ):
            raise ValueError("completion resolver must be synchronous and callable")
        if not isinstance(self.retry_safe, bool):
            raise ValueError("retry safety must be an explicit server-side boolean")
        if self.on_completed is not None and (
            not callable(self.on_completed) or inspect.iscoroutinefunction(self.on_completed)
        ):
            raise ValueError("result persistence hook must be synchronous and callable")

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError("tool name is already registered")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def schemas(self) -> list[dict]:
        return [definition.schema() for definition in self._definitions.values()]
