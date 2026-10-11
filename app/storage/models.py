"""Validated run inputs and stored records; timestamps are always aware UTC."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.config import Settings

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Preference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
StopReason = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,99}$")]


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="microseconds")


class StoredModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RunMode(StrEnum):
    PREVIEW = "preview"
    SCHEDULED = "scheduled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    NO_NEWS = "no_news"
    FAILED = "failed"


class SubscriptionSnapshot(StoredModel):
    nickname: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=254)
    topics: tuple[Preference, ...] = Field(min_length=1, max_length=20)
    keywords: tuple[Preference, ...] = Field(default=(), max_length=100)
    exclude_keywords: tuple[Preference, ...] = Field(default=(), max_length=100)
    max_items: int = Field(default=5, ge=1, le=5)
    enabled: bool = True
    push_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class RunRequest(StoredModel):
    user_id: Identifier
    subscription_id: Identifier
    subscription: SubscriptionSnapshot
    mode: RunMode = RunMode.PREVIEW
    scheduled_for: AwareDatetime | None = None

    @field_validator("scheduled_for")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        if (self.mode == RunMode.SCHEDULED) != (self.scheduled_for is not None):
            raise ValueError("only scheduled runs require scheduled_for")
        return self


class BudgetLimits(StoredModel):
    model_requests: int = Field(ge=2)
    searches: int = Field(ge=1)
    fetches: int = Field(ge=1)
    tool_calls: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            model_requests=settings.max_model_requests,
            searches=settings.max_search_calls,
            fetches=settings.max_fetch_calls,
            tool_calls=settings.max_tool_calls,
            timeout_seconds=settings.run_timeout_seconds,
        )


class BudgetUsage(StoredModel):
    model_requests: int = Field(default=0, ge=0)
    searches: int = Field(default=0, ge=0)
    fetches: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class BudgetCharge(BudgetUsage):
    @model_validator(mode="after")
    def require_charge(self) -> Self:
        if not any(self.model_dump().values()):
            raise ValueError("budget charge must increment at least one counter")
        return self


class StateChange(StoredModel):
    status: RunStatus
    retry_at: AwareDatetime | None = None
    stop_reason: StopReason | None = None

    @field_validator("retry_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_details(self) -> Self:
        if (self.status == RunStatus.WAITING_RETRY) != (self.retry_at is not None):
            raise ValueError("only waiting_retry requires retry_at")
        if (self.status == RunStatus.FAILED) != (self.stop_reason is not None):
            raise ValueError("only failed requires a stop_reason code")
        return self


class RunRecord(StoredModel):
    id: Identifier
    user_id: Identifier
    subscription_id: Identifier
    mode: RunMode
    status: RunStatus
    subscription: SubscriptionSnapshot
    budget_limits: BudgetLimits
    budget_usage: BudgetUsage
    created_at: AwareDatetime
    updated_at: AwareDatetime
    scheduled_for: AwareDatetime | None
    window_start: AwareDatetime
    window_end: AwareDatetime
    started_at: AwareDatetime | None
    deadline_at: AwareDatetime | None
    retry_at: AwareDatetime | None
    stop_reason: StopReason | None


class RunEvent(StoredModel):
    id: int
    run_id: Identifier
    kind: str
    occurred_at: AwareDatetime
    data: dict[str, JsonValue]


class EventPage(StoredModel):
    after_id: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
