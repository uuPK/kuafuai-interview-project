"""Provider adapters classify failures without exposing raw exception text."""

from typing import Literal

from pydantic import Field, model_validator

from app.tools.models import ToolParameters

FailureCategory = Literal[
    "transient", "authentication", "quota", "configuration", "protocol", "observation"
]
FATAL_CATEGORIES = {"authentication", "quota", "configuration", "protocol"}


class FailureInfo(ToolParameters):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    category: FailureCategory = "observation"
    retryable: bool = False
    retry_after: str | None = Field(default=None, max_length=200, repr=False)

    @model_validator(mode="after")
    def fatal_failures_do_not_retry(self):
        if self.category in FATAL_CATEGORIES:
            object.__setattr__(self, "retryable", False)
        return self


class RetryDeferredError(RuntimeError):
    """The retry is durable; return control instead of keeping the worker asleep."""


class RequestInProgressError(RuntimeError):
    """An in-flight attempt needs its executor or a recovery worker."""


class RetryNotDueError(RuntimeError):
    """The durable Retry-After/backoff time has not arrived."""
