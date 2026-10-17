"""Bounded model research notes; these never establish delivery or evidence quality."""

import json
from datetime import date
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.tools.models import ToolParameters

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Topic = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Fact = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class ResearchProgress(ToolParameters):
    collected: tuple[Topic, ...] = Field(
        max_length=20,
        description="已有正文材料的订阅原话题；填topics中的原值，不填新闻标题、事件或进度句子。",
    )
    missing: tuple[Topic, ...] = Field(
        max_length=20,
        description="仍待补充的订阅原话题；填topics中的原值，不填新闻标题、事件或进度句子。",
    )
    issues: tuple[ShortText, ...] = Field(
        max_length=10, description="材料不足、时间不明等具体问题；新闻或事件的说明放在这里。"
    )

    @field_validator("collected", "missing", "issues")
    @classmethod
    def unique_plain_text(cls, values):
        if len({value.casefold() for value in values}) != len(values) or any(
            ord(char) < 32 for value in values for char in value
        ):
            raise ValueError("progress lists require unique plain text")
        return values

    @model_validator(mode="after")
    def disjoint_topics(self):
        if {topic.casefold() for topic in self.collected} & {
            topic.casefold() for topic in self.missing
        }:
            raise ValueError("collected and missing topics must not overlap")
        return self


class HistoryReference(ToolParameters):
    source_id: str = Field(min_length=1, max_length=100)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=2000, repr=False)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_offset <= self.start_offset or len(self.quote) != (
            self.end_offset - self.start_offset
        ):
            raise ValueError("history quote must match its character range")
        return self


class HistoryEvent(ToolParameters):
    event_id: str = Field(min_length=1, max_length=200)
    title: ShortText
    topics: tuple[Topic, ...] = Field(min_length=1, max_length=20)
    entities: tuple[Topic, ...] = Field(default=(), max_length=10)
    product_version: str | None = Field(default=None, max_length=100)
    event_date: date | None = None
    key_facts: tuple[Fact, ...] = Field(min_length=1, max_length=10)
    citations: tuple[HistoryReference, ...] = Field(min_length=1, max_length=10)


class SelectionNote(ToolParameters):
    title: ShortText
    topics: tuple[Topic, ...] = Field(min_length=1, max_length=20)
    selection_reason: str = Field(min_length=1, max_length=500)


def selection_notes(content):
    """Project bounded reasons from a draft, without interpreting other fields as instructions."""
    try:
        value = json.loads(content)
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list) or len(items) > 5:
            return []
        notes = [
            SelectionNote.model_validate_json(
                json.dumps(
                    {key: item[key] for key in ("title", "topics", "selection_reason")},
                    ensure_ascii=False,
                )
            ).model_dump(mode="json")
            for item in items
        ]
        return notes
    except (ValueError, KeyError, TypeError, RecursionError):
        return []
