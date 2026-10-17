"""Model drafts and server-rendered evidence, with separate trust boundaries."""

from datetime import date
from typing import Annotated, Literal

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


class UnverifiedItem(ToolParameters):
    candidate_id: str = Field(pattern=r"^cand-[a-f0-9]{32}$")
    reason: Text = Field(
        max_length=500,
        description="未能确认的事实、来源矛盾或无法取得正文等具体原因；不要写成已证实结论。",
    )


class BriefDraft(ToolParameters):
    title: Text = Field(max_length=300)
    items: tuple[BriefItem, ...] = Field(max_length=5)
    uncovered_topics: tuple[UncoveredTopic, ...] = Field(default=(), max_length=20)
    shortfall_reason: Text | None = Field(default=None, max_length=500)
    unverified_items: tuple[UnverifiedItem, ...] = Field(default=(), max_length=5)


class SubmitParameters(ToolParameters):
    draft: BriefDraft


class PublishedCitation(BriefCitation):
    url: str
    content_hash: str
    published_at: AwareDatetime | None = None
    publication_status: Literal["confirmed", "missing", "ambiguous", "conflicting"] = "confirmed"


class PublishedItem(BriefItem):
    citations: tuple[PublishedCitation, ...] = Field(min_length=1, max_length=10)
    verification_notes: tuple[Text, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def require_time_notes(self):
        if (
            any(
                c.published_at is None or c.publication_status != "confirmed"
                for c in self.citations
            )
            and not self.verification_notes
        ):
            raise ValueError("unconfirmed publication requires a visible verification note")
        return self


class PublishedUnverifiedItem(UnverifiedItem):
    title: Text = Field(max_length=300)
    url: str


class BriefDocument(BriefDraft):
    items: tuple[PublishedItem, ...] = Field(max_length=5)
    unverified_items: tuple[PublishedUnverifiedItem, ...] = Field(default=(), max_length=5)


class ReviewOutcome(ToolParameters):
    approved: bool
    issues: tuple[ArgumentIssue, ...] = Field(max_length=10)

    @model_validator(mode="after")
    def consistent_decision(self):
        if self.approved == bool(self.issues):
            raise ValueError("review decision must agree with its issues")
        return self
