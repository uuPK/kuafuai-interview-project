"""Durable checked intent, followed by atomic brief, history, result and feedback."""

import json
from dataclasses import replace
from typing import Literal
from uuid import uuid4

from pydantic import Field

from app.agent.briefs import (
    BriefDocument,
    BriefDraft,
    PublishedUnverifiedItem,
    ReviewOutcome,
    SubmitParameters,
)
from app.agent.research import HistoryEvent, HistoryReference
from app.agent.unverified import retained_indices, review_notes, unverified_draft
from app.harness.failures import FATAL_CATEGORIES, FailureInfo
from app.storage.models import StateChange, StoredModel, timestamp
from app.storage.outbox import OutboxStore
from app.storage.runs import _record
from app.storage.snapshots import SourceMetadata
from app.tools.context import bind_context
from app.tools.models import ArgumentIssue, ToolFailure, ToolInvocation, ToolResult


class SubmissionJob(StoredModel):
    version: Literal[1, 2, 3] = 1
    brief_id: str = Field(pattern=r"^brief-[a-f0-9]{32}$")
    run_id: str
    tool_call_id: str
    invocation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_basis: Literal["fixture", "independent"]
    draft: BriefDraft
    sources: tuple[SourceMetadata, ...] = Field(max_length=50)
    duplicate_hints: tuple[dict, ...] = Field(max_length=100)
    unverified_items: tuple[PublishedUnverifiedItem, ...] = Field(default=(), max_length=5)
    fallback_document: BriefDocument | None = None
    review_issues: tuple[ArgumentIssue, ...] = Field(default=(), max_length=10)
    review_outcome: ReviewOutcome | None = None

    @property
    def result(self):
        return ToolResult.success(
            {
                "accepted": True,
                "brief_id": self.brief_id,
                "status": "completed"
                if self.draft.items or self.draft.unverified_items
                else "no_news",
                "review_basis": self.review_basis,
                **(
                    {"review_status": self.fallback_document.review_status}
                    if self.fallback_document
                    else {}
                ),
            }
        )


class BriefNotFoundError(LookupError):
    """Unknown and foreign briefs share one public outcome."""


class BriefStore:
    def __init__(self, research, rules, *, outbox=None):
        self.research, self.rules = research, rules
        self.runs, self.calls, self.database = research.runs, research.calls, research.database
        self.outbox = outbox or OutboxStore(self.runs)
        if self.outbox.runs is not self.runs:
            raise ValueError("brief and outbox must share their run store")

    @staticmethod
    def _problem():
        raise ToolFailure("BRIEF_STATE_PROBLEM", "简报提交意图或来源版本不一致，请核查存储。")

    def check_attempt(self, context):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            call = self.calls._row(connection, context.run_id, context.tool_call_id)
            if call["name"] != "submit_brief" or call["status"] != "executing":
                raise PermissionError("submission requires an executing call")
            count = self._attempts(connection, context.run_id)
            if count > 2:
                raise ToolFailure("BRIEF_SUBMISSION_LIMIT", "已达到两次提交上限，无法继续核对。")
            return count

    @staticmethod
    def _attempts(connection, run_id):
        return connection.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND name='submit_brief' "
            "AND started_at IS NOT NULL",
            (run_id,),
        ).fetchone()[0]

    def _job(self, row, call):
        try:
            job = SubmissionJob.model_validate_json(row["payload_json"])
            arguments = SubmitParameters.model_validate_json(call.arguments)
            fingerprint = ToolInvocation(
                tool_call_id=call.tool_call_id, name=call.name, arguments=call.arguments
            ).fingerprint
            if (
                job.run_id != call.run_id
                or job.tool_call_id != call.tool_call_id
                or job.invocation_hash != call.invocation_hash
                or fingerprint != call.invocation_hash
                or call.name != "submit_brief"
                or job.draft != arguments.draft
            ):
                self._problem()
            return job
        except (ValueError, TypeError):
            self._problem()

    def _validate(self, context, job):
        if job.version >= 3 and job.review_basis == "independent" and job.review_outcome is None:
            self._problem()
        checked, rejected = self.rules.check(
            context, job.draft, enrich_pending=job.version >= 2, auto_locate=job.version >= 3
        )
        if (
            rejected is not None
            or checked.sources != job.sources
            or checked.duplicate_hints != job.duplicate_hints
            or checked.document.unverified_items != job.unverified_items
        ):
            self._problem()
        if job.version >= 3 and job.review_outcome is not None:
            saved = self.saved_outcome(context, job.draft, checked)
            if saved is None and job.review_basis == "fixture":
                saved = job.review_outcome
            if saved != job.review_outcome:
                self._problem()
        if job.fallback_document is not None:
            document = self.unverified_document(
                context,
                job.draft,
                checked,
                job.review_issues,
                include_summaries=job.version >= 2,
                per_item=job.version >= 3,
            )
            if document is None or document != job.fallback_document:
                self._problem()
            checked = replace(checked, document=document)
        elif job.review_issues:
            self._problem()
        elif job.review_outcome is not None:
            if not job.review_outcome.approved:
                self._problem()
            checked = replace(checked, document=review_notes(checked.document, job.review_outcome))
        return checked

    def saved_outcome(self, context, draft, checked):
        from app.agent.protocol import ModelResponse
        from app.agent.review import (
            ReviewInput,
            ReviewInputProblem,
            canonical_draft,
            request_outcome,
        )
        from app.storage.reviews import ReviewStore

        request = ReviewStore(self.calls, self.rules.settings).saved_request(context)
        if request is None:
            reviewer = ReviewInput(self.research, self.rules.sources, self.rules.settings)
            try:
                history = reviewer.related_history(context, draft)
            except ReviewInputProblem:
                return None
            duplicates = reviewer.duplicate_issues(draft, history)
            if duplicates and len(duplicates) == len(draft.items):
                from app.agent.briefs import ItemReview

                return ReviewOutcome(
                    approved=False,
                    issues=duplicates,
                    item_reviews=tuple(
                        ItemReview(item_index=i, approved=False) for i in range(len(draft.items))
                    ),
                )
            return None
        try:
            packet = json.loads(request.messages[1].content)
            expected = (
                canonical_draft(draft, checked) if packet.get("review_protocol", 1) >= 2 else draft
            )
            saved_draft = BriefDraft.model_validate_json(json.dumps(packet["draft"]))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            self._problem()
        if saved_draft != expected:
            self._problem()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT response_json FROM brief_reviews WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return request_outcome(ModelResponse.model_validate_json(row[0]), draft, request)

    def unverified_document(
        self, context, draft, checked, issues, *, include_summaries=True, per_item=True
    ):
        # Accept a complete negative model review or the owned exact-history fact check.
        # Capacity, unsafe input, service and protocol failures retain their error path.
        from app.agent.protocol import ModelResponse
        from app.agent.review import ReviewInput, ReviewInputProblem, parse_review

        with self.database.connection() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            if self._attempts(connection, context.run_id) != 2:
                return None
            row = connection.execute(
                "SELECT response_json FROM brief_reviews WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
        if per_item:
            outcome = self.saved_outcome(context, draft, checked)
            if outcome is None:
                return None
        elif row is None:
            reviewer = ReviewInput(self.research, self.rules.sources, self.rules.settings)
            try:
                history = reviewer.related_history(context, draft)
            except ReviewInputProblem:
                return None
            try:
                reviewer.unchanged_facts(draft, history)
            except ReviewInputProblem as problem:
                outcome = problem.outcome
            else:
                return None
        else:
            if row[0] is None:
                return None
            try:
                outcome = parse_review(ModelResponse.model_validate_json(row[0]), draft)
            except (ValueError, ToolFailure):
                self._problem()
        if outcome.approved or outcome.issues != tuple(issues):
            self._problem()
        run = self.runs.get_run(context.run_id, context.user_id)
        keep = retained_indices(draft, checked, outcome) if per_item else ()
        proposed = unverified_draft(
            draft,
            checked,
            issues,
            run.subscription.topics,
            include_summaries=include_summaries,
            keep_indices=keep,
            preserve_pending=per_item,
        )
        if proposed is None:
            return None
        pending, rejected = self.rules.check(
            context, proposed, enrich_pending=include_summaries, auto_locate=per_item
        )
        if rejected is not None:
            return None
        document = BriefDocument(
            **{
                **pending.document.model_dump(),
                "review_status": "partial" if keep else "unverified",
            }
        )
        return review_notes(document, outcome, keep) if per_item else document

    def prepared(self, context, draft):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, context.run_id, context.user_id)
            row = connection.execute(
                "SELECT payload_json FROM submission_jobs WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
        if row is None:
            return None
        call = self.calls.get_call(context.run_id, context.user_id, context.tool_call_id)
        job = self._job(row, call)
        if job.draft != draft:
            self._problem()
        self._validate(context, job)
        return job.result

    def stage(
        self,
        context,
        draft,
        checked,
        review_basis,
        *,
        fallback_document=None,
        review_issues=(),
        review_outcome=None,
    ):
        call = self.calls.get_call(context.run_id, context.user_id, context.tool_call_id)
        if call.status != "executing" or call.name != "submit_brief":
            raise PermissionError("submission intent requires an executing server-bound call")
        job = SubmissionJob(
            version=3,
            brief_id="brief-" + uuid4().hex,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            invocation_hash=call.invocation_hash,
            draft=draft,
            sources=checked.sources,
            duplicate_hints=checked.duplicate_hints,
            unverified_items=checked.document.unverified_items,
            review_basis=review_basis,
            fallback_document=fallback_document,
            review_issues=review_issues,
            review_outcome=review_outcome,
        )
        if len(job.result.model_dump_json()) > self.rules.settings.max_tool_result_chars:
            raise ToolFailure("RESULT_TOO_LARGE", "提交确认无法容纳，请检查结果大小配置。")
        with self.database.transaction() as connection:
            run = self.runs._owned_row(connection, context.run_id, context.user_id)
            if run["status"] != "running":
                raise PermissionError("submission intent requires a running run")
            row = connection.execute(
                "SELECT payload_json FROM submission_jobs WHERE run_id=? AND tool_call_id=?",
                (context.run_id, context.tool_call_id),
            ).fetchone()
            if row is not None:
                saved = self._job(row, call)
                if (
                    saved.draft != draft
                    or saved.sources != checked.sources
                    or saved.review_basis != review_basis
                    or saved.unverified_items != checked.document.unverified_items
                    or saved.fallback_document != fallback_document
                    or saved.review_issues != tuple(review_issues)
                    or saved.review_outcome != review_outcome
                ):
                    self._problem()
                return saved.result
            connection.execute(
                "INSERT INTO submission_jobs VALUES (?, ?, ?)",
                (context.run_id, context.tool_call_id, job.model_dump_json()),
            )
        return job.result

    def on_completed(self, connection, call):
        if not call.result.ok:
            run = _record(
                connection.execute("SELECT * FROM runs WHERE id=?", (call.run_id,)).fetchone()
            )
            attempts = self._attempts(connection, call.run_id)
            self.runs._append_event(
                connection,
                call.run_id,
                "brief_check_failed",
                self.runs.now(),
                {
                    "tool_call_id": call.tool_call_id,
                    "error_code": call.result.error.code,
                    "issues": [issue.model_dump() for issue in call.result.error.details],
                    "submission_attempts": attempts,
                    "revisions_remaining": max(0, 2 - attempts),
                },
            )
            code = call.result.error.code
            request = connection.execute(
                "SELECT failure_json FROM external_requests WHERE run_id=? AND tool_call_id=?",
                (call.run_id, call.tool_call_id),
            ).fetchone()
            failure = (
                FailureInfo.model_validate_json(request[0]) if request and request[0] else None
            )
            dependency = code in {
                "BRIEF_REVIEW_SERVICE_FAILED",
                "BRIEF_REVIEW_TIMEOUT",
                "TOOL_TIMEOUT",
            }
            fatal = failure is not None and failure.category in FATAL_CATEGORIES
            if run.status == "running" and (dependency or fatal or attempts >= 2):
                reason = "brief_review_service_failed" if dependency else "brief_revision_exhausted"
                if fatal:
                    reason = f"tool_dependency_{failure.category}"
                # Budget reasons are set by the paired-feedback hook.
                if code != "BUDGET_EXCEEDED":
                    self.runs._transition(
                        connection,
                        run,
                        StateChange(status="failed", stop_reason=reason),
                        self.runs.now(),
                    )
            return
        row = connection.execute(
            "SELECT payload_json FROM submission_jobs WHERE run_id=? AND tool_call_id=?",
            (call.run_id, call.tool_call_id),
        ).fetchone()
        if row is None:
            self._problem()
        job = self._job(row, call)
        if call.result != job.result:
            self._problem()
        run = _record(
            connection.execute("SELECT * FROM runs WHERE id=?", (call.run_id,)).fetchone()
        )
        context = bind_context(
            self.runs,
            self.rules.settings.data_dir,
            run.id,
            run.user_id,
            tool_call_id=call.tool_call_id,
        )
        checked = self._validate(context, job)
        at = self.runs.now()
        self.runs._transition(connection, run, StateChange(status=job.result.data["status"]), at)
        connection.execute(
            "INSERT INTO briefs VALUES (?, ?, ?, ?, ?)",
            (
                job.brief_id,
                run.id,
                timestamp(at),
                job.review_basis,
                checked.document.model_dump_json(),
            ),
        )
        if checked.document.items:
            events = tuple(
                HistoryEvent.model_validate_json(
                    json.dumps(
                        {
                            "citations": [
                                {
                                    key: value
                                    for key, value in ref.model_dump(mode="json").items()
                                    if key in HistoryReference.model_fields
                                }
                                for ref in item.citations
                            ],
                            **{
                                key: value
                                for key, value in item.model_dump(mode="json").items()
                                if key in HistoryEvent.model_fields and key != "citations"
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                for item in checked.document.items
            )
            self.research.record_delivery(connection, run.id, run.user_id, job.brief_id, events)
        self.outbox.reserve(connection, run.id, run.user_id, job.brief_id)
        self.runs._append_event(
            connection,
            run.id,
            "brief_saved",
            at,
            {
                "tool_call_id": call.tool_call_id,
                "brief_id": job.brief_id,
                "item_count": len(checked.document.items),
                "review_basis": job.review_basis,
                "duplicate_hints": list(checked.duplicate_hints),
                "citation_adjustments": list(checked.citation_adjustments),
                "items": [
                    {"title": item.title, "selection_reason": item.selection_reason}
                    for item in checked.document.items
                ],
                "basis": (
                    "server_item_review"
                    if checked.document.review_status == "partial"
                    else "server_unverified_fallback"
                )
                if job.fallback_document
                else "model_reported",
                "review_status": checked.document.review_status,
            },
        )

    def get(self, brief_id, user_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT b.* FROM briefs b JOIN runs r ON r.id=b.run_id "
                "WHERE b.id=? AND r.user_id=?",
                (brief_id, user_id),
            ).fetchone()
        if row is None:
            raise BriefNotFoundError("brief not found for this user")
        try:
            return {
                "brief_id": row["id"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "review_basis": row["review_basis"],
                "document": self._reading_document(row, user_id),
            }
        except ValueError:
            self._problem()

    def _reading_document(self, row, owner):
        """Enrich legacy reading views without rewriting frozen delivery/recovery intent."""
        document = BriefDocument.model_validate_json(row["document_json"])
        if not any(item.summary is None for item in document.unverified_items):
            return document
        with self.database.connection() as connection:
            intent = connection.execute(
                "SELECT payload_json FROM submission_jobs WHERE run_id=? "
                "AND json_extract(payload_json,'$.brief_id')=?",
                (row["run_id"], row["id"]),
            ).fetchone()
        originals = {}
        if intent:
            raw = SubmissionJob.model_validate_json(intent[0])
            call = self.calls.get_call(row["run_id"], owner, raw.tool_call_id)
            job = self._job(intent, call)
            sources = {source.source_id: source for source in job.sources}
            for item in job.draft.items:
                source = sources.get(item.citations[0].source_id)
                if source:
                    originals[(source.candidate_id, source.url)] = item
        pending = []
        for item in document.unverified_items:
            if item.summary is not None:
                pending.append(item)
                continue
            original = originals.get((item.candidate_id, item.url))
            summary, topics = (
                (original.summary, original.topics) if original else (None, item.topics)
            )
            if not summary:
                try:
                    candidate = self.rules.sources.candidates.get_candidate(
                        row["run_id"], owner, item.candidate_id
                    )
                except LookupError:
                    candidate = None
                if candidate and candidate.material.url == item.url:
                    context = bind_context(
                        self.runs, self.rules.settings.data_dir, row["run_id"], owner
                    )
                    summary = self.rules.pending_summary(context, candidate, item.url)
            if summary:
                summary = self.rules.redactor.clean(summary)
                run = self.runs.get_run(row["run_id"], owner)
                if any(
                    word.casefold() in summary.casefold()
                    for word in run.subscription.exclude_keywords
                ):
                    summary = None
            pending.append(item.model_copy(update={"summary": summary, "topics": topics}))
        return document.model_copy(update={"unverified_items": tuple(pending)})
