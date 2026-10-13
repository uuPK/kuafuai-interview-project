"""Model drafts and server-rendered evidence, with separate trust boundaries."""

from datetime import date
from typing import Annotated

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.agent.research import Fact, Topic
from app.tools.models import ArgumentIssue, ToolParameters

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class BriefCitation(ToolParameters):
    source_id: str = Field(pattern=r"^src-[a-f0-9]{32}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=2000, repr=False)


class BriefItem(ToolParameters):
    title: Text = Field(max_length=300)
    summary: Text = Field(max_length=2000)
    topics: tuple[Topic, ...] = Field(min_length=1, max_length=20)
    event_id: Text = Field(max_length=200)
    selection_reason: Text = Field(max_length=500)
    entities: tuple[Topic, ...] = Field(default=(), max_length=10)
    product_version: Text | None = Field(default=None, max_length=100)
    event_date: date | None = None
    key_facts: tuple[Fact, ...] = Field(min_length=1, max_length=10)
    citations: tuple[BriefCitation, ...] = Field(min_length=1, max_length=10)


class UncoveredTopic(ToolParameters):
    topic: Topic
    reason: Text = Field(max_length=500)


class BriefDraft(ToolParameters):
    title: Text = Field(max_length=300)
    items: tuple[BriefItem, ...] = Field(max_length=5)
    uncovered_topics: tuple[UncoveredTopic, ...] = Field(default=(), max_length=20)
    shortfall_reason: Text | None = Field(default=None, max_length=500)


class SubmitParameters(ToolParameters):
    draft: BriefDraft


class PublishedCitation(BriefCitation):
    url: str
    content_hash: str
    published_at: AwareDatetime


class PublishedItem(BriefItem):
    citations: tuple[PublishedCitation, ...] = Field(min_length=1, max_length=10)


class BriefDocument(BriefDraft):
    items: tuple[PublishedItem, ...] = Field(max_length=5)


class ReviewOutcome(ToolParameters):
    approved: bool
    issues: tuple[ArgumentIssue, ...] = Field(max_length=10)

    @model_validator(mode="after")
    def consistent_decision(self):
        if self.approved == bool(self.issues):
            raise ValueError("review decision must agree with its issues")
        return self
