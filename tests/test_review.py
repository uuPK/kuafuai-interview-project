import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest

import app.storage.database as database_module
from app.agent.briefs import BriefDraft, SubmitParameters
from app.agent.prompts import initial_messages
from app.agent.protocol import AssistantMessage, ModelFailure, ModelRequest, ModelResponse
from app.agent.review import ReviewInputProblem, parse_review
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.storage.conversations import ConversationStore
from app.storage.models import BudgetCharge
from app.tools.context import bind_context
from app.tools.models import ToolFailure, ToolInvocation
from app.tools.registry import ToolDefinition
from tests.brief_support import checked, draft, draft_item, review_world
from tests.test_briefs import brief_env as _brief_env
from tests.test_briefs import counts, execute, invocation
from tests.test_harness import Clock
from tests.test_research import seed_history

brief_env = _brief_env


class Interrupted(BaseException):
    pass


class ReviewModel:
    fixture = True

    def __init__(self, *responses):
        self.responses = list(responses) or [{"approved": True, "issues": []}]
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, ModelResponse):
            return value
        return ModelResponse(
            message=AssistantMessage(content=json.dumps(value, ensure_ascii=False)),
            usage={"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
        )


def reject(field="draft.items[0].summary", issue="原文为23%，没有支持99%的数字"):
    return {"approved": False, "issues": [{"field": field, "issue": issue}]}


def run_submit(world, value, call="submit", **kwargs):
    env = world.env
    return world.executor.execute(env.run.id, env.run.user_id, invocation(value, call), **kwargs)


def test_server_context_keeps_units_conditions_and_owned_history(tool_env):
    async def run():
        env, model = tool_env, ReviewModel()
        unrelated = await seed_history(
            env, topics=("AI芯片",), event_id="chip", facts=("400 GB/s",), brief_id="unrelated"
        )
        relevant = await seed_history(
            env, facts=("Atlas v1 已发布",), event_id="atlas-v1", brief_id="relevant"
        )
        await seed_history(env, user="other", event_id="atlas-v2", brief_id="foreign")
        await seed_history(env, age=8, event_id="atlas-v0", brief_id="old")
        async with review_world(env, model) as world:
            item = await draft_item(world)
            ref = item["citations"][0]
            relative = ref["quote"].index("23%")
            ref.update(
                start_offset=ref["start_offset"] + relative,
                end_offset=ref["start_offset"] + relative + 3,
                quote="23%",
            )
            value = draft(item)
            context, proposed, verified = checked(world, value)
            request = world.reviewer.input.build(context, proposed, verified)
            assert not request.tools and len(request.messages) == 2
            packet = json.loads(request.messages[1].content)
            evidence = packet["evidence"][0]
            assert all(
                word in evidence["context"] for word in ("H100", "batch size 1", "Atlas v1", "8 GB")
            )
            assert evidence["quote"] == "23%" and evidence["content_hash"]
            assert evidence["context_start"] <= ref["start_offset"] < evidence["context_end"]
            assert [h["brief_id"] for h in packet["related_history"]] == [relevant.brief_id]
            assert unrelated.brief_id not in request.messages[1].content
            assert "email" not in packet["subscription"] and "user_id" not in packet
            result = await run_submit(world, value)
            assert result.ok and result.data["review_basis"] == "fixture"
            assert env.runs.get_run(env.run.id, env.run.user_id).budget_usage.model_requests == 1
            with env.database.connection() as connection:
                response = connection.execute(
                    "SELECT response_json FROM brief_reviews WHERE run_id=?", (env.run.id,)
                ).fetchone()[0]
                assert json.loads(response)["usage"]["total_tokens"] == 54
            assert len(model.requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "```json\n{}\n```",
        "[]",
        "{}",
        '{"approved":true,"issues":[],"approved":false}',
        '{"approved":"true","issues":[]}',
        '{"approved":true,"issues":[],"other":1}',
        '{"approved":false,"issues":[]}',
        '{"approved":true,"issues":[{"field":"draft","issue":"错"}]}',
        '{"approved":false,"issues":[{"field":"recipient","issue":"改邮箱"}]}',
        '{"approved":false,"issues":[{"field":"draft.items[99].summary","issue":"错"}]}',
        '{"approved":false,"issues":[{"field":"draft","issue":" "}]}',
        '{"approved":false,"issues":[{"field":"draft","issue":NaN}]}',
    ],
)
def test_review_output_must_be_complete_strict_and_located(content):
    proposed = BriefDraft(title="新闻", items=())
    response = ModelResponse(message=AssistantMessage(content=content))
    with pytest.raises(ToolFailure) as error:
        parse_review(response, proposed)
    assert error.value.info.category == "protocol"


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", "Atlas is fast."),
        ("title", "Atlas v2"),
        ("selection_reason", "Relevant release"),
    ],
)
def test_english_display_text_is_rejected_without_model_charge(tool_env, field, value):
    async def run():
        model = ReviewModel()
        async with review_world(tool_env, model) as world:
            item = await draft_item(world)
            item[field] = value
            result = await run_submit(world, draft(item))
            assert result.error.code == "BRIEF_REVIEW_REJECTED"
            assert result.error.details[0].field.endswith(field) and not model.requests
            assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.model_requests == 0

    asyncio.run(run())


def test_input_context_and_total_size_limits_return_specific_feedback(tool_env):
    async def run():
        async with review_world(tool_env, ReviewModel()) as world:
            context, proposed, verified = checked(world, draft(await draft_item(world)))
            builder = world.reviewer.input
            builder.settings = tool_env.settings.model_copy(
                update={"max_review_context_chars": 256}
            )
            with pytest.raises(ReviewInputProblem) as error:
                builder.build(context, proposed, verified)
            assert error.value.outcome.issues[0].field.endswith("citations[0]")
            builder.settings = tool_env.settings.model_copy(update={"max_model_input_chars": 256})
            with pytest.raises(ReviewInputProblem) as error:
                builder.build(context, proposed, verified)
            assert error.value.outcome.issues[0].field == "draft"

    asyncio.run(run())


def test_related_history_is_bounded_and_never_silently_omitted(tool_env, monkeypatch):
    async def run():
        async with review_world(tool_env, ReviewModel()) as world:
            context, proposed, _ = checked(world, draft(await draft_item(world)))
            builder = world.reviewer.input
            historical = await seed_history(tool_env)
            events, _ = world.research.history(context, 7, 20)
            monkeypatch.setattr(world.research, "history", lambda *args: (events, 201))
            with pytest.raises(ReviewInputProblem):
                builder.related_history(context, proposed)
            monkeypatch.setattr(world.research, "history", lambda *args: (events * 51, 51))
            with pytest.raises(ReviewInputProblem):
                builder.related_history(context, proposed)
            assert historical.event.topics

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["none", "reordered", "new_fact", "new_version", "other_id", "no_version"]
)
def test_exact_delivered_facts_rejected_but_semantic_cases_reach_reviewer(tool_env, change):
    async def run():
        env, model = tool_env, ReviewModel()
        async with review_world(env, model) as world:
            item = await draft_item(world)
            historical = list(item["key_facts"])
            await seed_history(env, facts=tuple(historical), event_id="atlas-v2", version="v2")
            if change == "reordered":
                item["key_facts"] = list(reversed(historical))
            elif change == "new_fact":
                item["key_facts"] = [*historical, "权重现可获取"]
            elif change == "new_version":
                item["product_version"] = "v3"
            elif change == "other_id":
                item["event_id"] = "another-event"
            elif change == "no_version":
                item["product_version"] = None
            result = await run_submit(world, draft(item))
            if change in {"none", "reordered"}:
                assert result.error.code == "BRIEF_REVIEW_REJECTED" and not model.requests
                assert result.error.details[0].field == "draft.items[0].key_facts"
                assert (
                    env.runs.get_run(env.run.id, env.run.user_id).budget_usage.model_requests == 0
                )
            else:
                assert len(model.requests) == 1

    asyncio.run(run())


def test_two_failed_submissions_stop_and_batch_ids_still_pair(brief_env, response_factory):
    ready, env = brief_env, brief_env.env
    bad = deepcopy(ready.draft)
    bad["items"][0]["citations"][0]["quote"] = "伪造"
    conversations = ConversationStore(env.calls)
    model = ScriptedModel(
        [
            response_factory(("s1", "submit_brief", {"draft": bad})),
            response_factory(
                ("s2", "submit_brief", {"draft": bad}),
                ("s3", "submit_brief", {"draft": ready.draft}),
            ),
        ]
    )
    done = asyncio.run(
        AgentRunner(model, ready.executor, conversations, env.settings).run(env.run.id, "user-1")
    )
    assert done.status == "failed" and done.stop_reason == "brief_revision_exhausted"
    assert env.calls.get_call(env.run.id, "user-1", "s3").result.error.code == "RUN_FINISHED"
    ModelRequest(messages=conversations.messages(env.run.id, "user-1"), tools=())
    assert counts(ready) == (0, 0, 0)


def test_schema_failure_counts_and_duplicate_id_does_not(brief_env):
    ready, env = brief_env, brief_env.env
    bad = ToolInvocation(tool_call_id="invalid", name="submit_brief", arguments="{}")
    first = asyncio.run(ready.executor.execute(env.run.id, "user-1", bad))
    assert first.error.code == "INVALID_ARGUMENTS"
    assert asyncio.run(ready.executor.execute(env.run.id, "user-1", bad)) == first
    assert (
        execute(ready, draft={"title": "bad"}, call_id="invalid2").error.code == "INVALID_ARGUMENTS"
    )
    done = env.runs.get_run(env.run.id, "user-1")
    assert done.stop_reason == "brief_revision_exhausted"
    events = [
        e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "brief_check_failed"
    ]
    assert [e.data["submission_attempts"] for e in events] == [1, 2]


def test_agent_revises_semantic_issue_and_only_checks_twice(tool_env, response_factory):
    async def run():
        model = ReviewModel(reject(), {"approved": True, "issues": []})
        async with review_world(tool_env, model) as world:
            good = draft(await draft_item(world))
            bad = deepcopy(good)
            bad["items"][0]["summary"] = "Atlas v2 吞吐量提高99%。"

            def revise(request):
                result = json.loads(request.messages[-1].content)
                assert result["error"]["code"] == "BRIEF_REVIEW_REJECTED"
                assert "23%" in result["error"]["details"][0]["issue"]
                return response_factory(("s2", "submit_brief", {"draft": good}))

            research = ScriptedModel(
                [response_factory(("s1", "submit_brief", {"draft": bad})), revise]
            )
            conversations = ConversationStore(tool_env.calls)
            done = await AgentRunner(
                research, world.executor, conversations, tool_env.settings
            ).run(tool_env.run.id, "user-1")
            assert done.status == "completed" and done.budget_usage.model_requests == 4
            assert len(model.requests) == 2
            assert len(conversations.responses(done.id, "user-1")) == 2

    asyncio.run(run())


def test_retry_after_is_durable_charges_each_model_attempt_and_one_submission(tool_env):
    async def run():
        env, clock = tool_env, Clock()
        clock.at = env.runs.now()
        env.runs.clock = clock.now
        config = env.settings.model_copy(update={"retry_inline_max_seconds": 0})
        env.settings = config
        model = ReviewModel(
            ModelFailure("DEEPSEEK_RATE_LIMIT", retryable=True, retry_after="10"),
            {"approved": True, "issues": []},
        )
        async with review_world(env, model) as world:
            item = await draft_item(world)
            conversations = ConversationStore(env.calls)
            research = ScriptedModel([ModelResponse(message=AssistantMessage(content="unused"))])
            conversations.initialize(env.run.id, "user-1", initial_messages(env.run))
            from app.agent.protocol import FunctionCall, ModelToolCall

            conversations.save_response(
                env.run.id,
                "user-1",
                ModelResponse(
                    message=AssistantMessage(
                        tool_calls=(
                            ModelToolCall(
                                id="s1",
                                function=FunctionCall(
                                    name="submit_brief",
                                    arguments=invocation(draft(item), "s1").arguments,
                                ),
                            ),
                        )
                    ),
                    finish_reason="tool_calls",
                ),
            )
            runner = AgentRunner(research, world.executor, conversations, config)
            waiting = await runner.run(env.run.id, "user-1")
            assert waiting.status == "waiting_retry" and waiting.budget_usage.model_requests == 1
            clock.advance(10)
            done = await runner.run(env.run.id, "user-1")
            assert done.status == "completed" and done.budget_usage.model_requests == 2
            request = world.executor.requests.for_tool(env.run.id, "user-1", "s1")
            assert request.attempts == 2 and len(model.requests) == 2
            assert model.requests[0] == model.requests[1]
            assert done.window_end == waiting.window_end

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure,reason",
    [
        (ModelFailure("AUTH", category="authentication"), "tool_dependency_authentication"),
        (ModelFailure("BAD", category="protocol"), "tool_dependency_protocol"),
        (ModelFailure("DOWN", retryable=True), "brief_review_service_failed"),
    ],
)
def test_review_service_failure_never_becomes_news_or_approval(tool_env, failure, reason):
    async def run():
        model = ReviewModel(failure, failure, failure)
        async with review_world(tool_env, model) as world:
            clock = Clock()
            clock.at = tool_env.runs.now()
            tool_env.runs.clock = clock.now
            world.executor.sleep = clock.sleep
            result = await run_submit(world, draft(await draft_item(world)))
            assert not result.ok
            done = tool_env.runs.get_run(tool_env.run.id, "user-1")
            assert done.status == "failed" and done.stop_reason == reason
            assert done.budget_usage.model_requests == (3 if failure.retryable else 1)
            with tool_env.database.connection() as connection:
                assert connection.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 0

    asyncio.run(run())


def test_review_protocol_failure_is_fatal(tool_env):
    async def run():
        async with review_world(tool_env, ReviewModel({"approved": False, "issues": []})) as world:
            result = await run_submit(world, draft(await draft_item(world)))
            assert result.error.code == "BRIEF_REVIEW_PROTOCOL"
            assert (
                tool_env.runs.get_run(tool_env.run.id, "user-1").stop_reason
                == "tool_dependency_protocol"
            )

    asyncio.run(run())


def test_saved_review_reused_after_stage_interruption_without_second_model_charge(
    tool_env, monkeypatch
):
    async def run():
        model = ReviewModel()
        async with review_world(tool_env, model) as world:
            value = draft(await draft_item(world))
            original = world.briefs.stage

            monkeypatch.setattr(
                world.briefs, "stage", lambda *args: (_ for _ in ()).throw(Interrupted())
            )
            with pytest.raises(Interrupted):
                await run_submit(world, value)
            monkeypatch.setattr(world.briefs, "stage", original)
            context = bind_context(
                tool_env.runs,
                tool_env.settings.data_dir,
                tool_env.run.id,
                "user-1",
                tool_call_id="submit",
            )
            result = await world.registry.get("submit_brief").handler(
                context, SubmitParameters.model_validate_json(invocation(value).arguments)
            )
            world.executor.finish(tool_env.run.id, "user-1", invocation(value), result)
            assert result.ok and len(model.requests) == 1
            assert tool_env.runs.get_run(tool_env.run.id, "user-1").budget_usage.model_requests == 1

    asyncio.run(run())


def test_submission_reserves_model_budget_before_last_research_request(tool_env, response_factory):
    async def run():
        env = tool_env
        async with review_world(env, ReviewModel()) as world:
            value = draft(await draft_item(world))
            remaining = (
                env.run.budget_limits.model_requests
                - env.runs.get_run(env.run.id, "user-1").budget_usage.model_requests
            )
            env.runs.consume_budget(
                env.run.id, "user-1", BudgetCharge(model_requests=remaining - 2)
            )
            research = ScriptedModel([response_factory(("s1", "submit_brief", {"draft": value}))])
            done = await AgentRunner(
                research, world.executor, ConversationStore(env.calls), env.settings
            ).run(env.run.id, "user-1")
            assert (
                done.status == "completed"
                and done.budget_usage.model_requests == done.budget_limits.model_requests
            )

    asyncio.run(run())


@pytest.mark.parametrize("reserve", [True, -1, 2, "1"])
def test_review_model_reserve_is_trusted_and_validated(reserve):
    from app.tools.models import ToolParameters

    async def handler(*args):
        pass

    with pytest.raises(ValueError):
        ToolDefinition("x", "test", ToolParameters, handler, reserve_model_requests=reserve)


def test_v10_migration_preserves_briefs_and_rolls_back(brief_env, monkeypatch):
    ready, db = brief_env, brief_env.env.database
    result = execute(ready)
    before = ready.store.get(result.data["brief_id"], "user-1")
    with db.connection() as connection:
        connection.execute("DROP TABLE mail_outbox")
        for table in (
            "mail_attempts",
            "mail_dispatch",
            "schedule_notices",
            "subscription_schedules",
            "scheduled_runs",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE subscriptions")
        connection.execute("DROP TABLE auth_sessions")
        connection.execute("DROP TABLE auth_users")
        connection.execute("DROP TABLE auth_attempts")
        connection.execute("DROP TABLE run_leases")
        connection.execute("DROP TABLE worker_instances")
        connection.execute("DROP TABLE brief_reviews")
        connection.execute("PRAGMA user_version=9")
    schema = database_module.SCHEMA_V10
    monkeypatch.setattr(database_module, "SCHEMA_V10", (*schema, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        db.initialize()
    with db.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='brief_reviews'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V10", schema)
    db.initialize()
    db.initialize()
    assert ready.store.get(result.data["brief_id"], "user-1") == before


@pytest.mark.parametrize("finish", ["length", "tool_calls", "content_filter"])
def test_review_incomplete_response_is_rejected(finish):
    proposed = BriefDraft(title="新闻", items=())
    response = ModelResponse(
        message=AssistantMessage(content='{"approved":true,"issues":[]}'), finish_reason=finish
    )
    with pytest.raises(ToolFailure):
        parse_review(response, proposed)


def test_model_cannot_request_tools_in_independent_review(response_factory):
    response = response_factory(
        ("bad", "bash", {"command": "ls"}), content='{"approved":true,"issues":[]}'
    )
    with pytest.raises(ToolFailure):
        parse_review(response, BriefDraft(title="新闻", items=()))


@pytest.mark.parametrize(
    "damage", ["invocation_hash", "input_hash", "request_json", "response_json"]
)
def test_corrupt_saved_review_cannot_be_reused(tool_env, monkeypatch, damage):
    async def run():
        model = ReviewModel()
        async with review_world(tool_env, model) as world:
            value = draft(await draft_item(world))
            original = world.briefs.stage
            monkeypatch.setattr(
                world.briefs, "stage", lambda *args: (_ for _ in ()).throw(Interrupted())
            )
            with pytest.raises(Interrupted):
                await run_submit(world, value)
            with tool_env.database.connection() as connection:
                connection.execute(f"UPDATE brief_reviews SET {damage}=?", ("broken",))
            monkeypatch.setattr(world.briefs, "stage", original)
            context = bind_context(
                tool_env.runs,
                tool_env.settings.data_dir,
                tool_env.run.id,
                "user-1",
                tool_call_id="submit",
            )
            with pytest.raises(ToolFailure) as error:
                await world.registry.get("submit_brief").handler(
                    context, SubmitParameters.model_validate_json(invocation(value).arguments)
                )
            assert error.value.result.error.code == "BRIEF_REVIEW_STATE_PROBLEM"
            assert len(model.requests) == 1

    asyncio.run(run())


def test_invalid_model_review_is_kept_for_controlled_audit(tool_env):
    async def run():
        response = ModelResponse(
            message=AssistantMessage(content="not JSON", reasoning_content="fixture-deep-key"),
            raw={"authorization": "fixture-deep-key"},
        )
        async with review_world(tool_env, ReviewModel(response)) as world:
            result = await run_submit(world, draft(await draft_item(world)))
            assert result.error.code == "BRIEF_REVIEW_PROTOCOL"
            with tool_env.database.connection() as connection:
                saved = connection.execute(
                    "SELECT response_json FROM brief_reviews WHERE run_id=?", (tool_env.run.id,)
                ).fetchone()[0]
            assert "fixture-deep-key" not in saved and "not JSON" in saved
            events = [
                e
                for e in tool_env.runs.list_events(tool_env.run.id, "user-1")
                if e.kind == "brief_review_completed"
            ]
            assert events[0].data["response_valid"] is False
            assert events[0].data["approved"] is None

    asyncio.run(run())


def test_review_input_never_sends_subscription_secret(tool_env):
    async def run():
        async with review_world(tool_env, ReviewModel()) as world:
            value = draft(await draft_item(world))
            with tool_env.database.connection() as connection:
                subscription = tool_env.run.subscription.model_copy(
                    update={"keywords": ("fixture-deep-key",)}
                )
                connection.execute(
                    "UPDATE runs SET subscription_json=? WHERE id=?",
                    (subscription.model_dump_json(), tool_env.run.id),
                )
            result = await run_submit(world, value)
            assert result.error.code == "BRIEF_REVIEW_REJECTED"
            assert "凭据" in result.error.details[0].issue
            assert not world.reviewer.model.requests

    asyncio.run(run())


def test_empty_news_has_discovery_evidence_and_independent_decision(tool_env):
    from tests.test_briefs import empty_draft

    async def run():
        model = ReviewModel()
        async with review_world(tool_env, model, zero_first=True) as world:
            await world.executor.execute(
                tool_env.run.id,
                "user-1",
                ToolInvocation(
                    tool_call_id="empty", name="web_search", arguments='{"query":"Atlas release"}'
                ),
            )
            result = await run_submit(world, empty_draft())
            assert result.ok and result.data["status"] == "no_news"
            packet = json.loads(model.requests[0].messages[1].content)
            state = packet["research_observations"]
            assert state["discoveries"][0]["result"]["data"]["items"] == []
            assert state["materials"] == [] and packet["related_history"] == []
            with tool_env.database.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM delivered_history").fetchone()[0] == 0
                )

    asyncio.run(run())


def test_no_budget_for_review_returns_failure_without_http(tool_env, response_factory):
    async def run():
        env, model = tool_env, ReviewModel()
        async with review_world(env, model) as world:
            value = draft(await draft_item(world))
            env.runs.consume_budget(
                env.run.id,
                "user-1",
                BudgetCharge(model_requests=env.run.budget_limits.model_requests),
            )
            conversations = ConversationStore(env.calls)
            conversations.initialize(env.run.id, "user-1", initial_messages(env.run))
            conversations.save_response(
                env.run.id, "user-1", response_factory(("last", "submit_brief", {"draft": value}))
            )
            done = await AgentRunner(
                ScriptedModel([]), world.executor, conversations, env.settings
            ).run(env.run.id, "user-1")
            assert done.status == "failed" and not model.requests
            assert (
                env.calls.get_call(env.run.id, "user-1", "last").result.error.code
                == "BUDGET_EXCEEDED"
            )

    asyncio.run(run())


def test_expired_review_is_not_approved(tool_env):
    async def run():
        model = ReviewModel()
        async with review_world(tool_env, model) as world:
            value = draft(await draft_item(world))
            config = tool_env.settings.model_copy(
                update={"request_timeout_seconds": 0.01, "external_max_attempts": 1}
            )
            world.executor.settings = config

            async def slow(request):
                await asyncio.sleep(1)

            model.complete = slow
            result = await run_submit(world, value)
            assert result.error.code == "TOOL_TIMEOUT"
            assert (
                tool_env.runs.get_run(tool_env.run.id, "user-1").stop_reason
                == "brief_review_service_failed"
            )

    asyncio.run(run())


def test_cancellation_stays_unfinished_and_requires_recovery(tool_env):
    async def run():
        model = ReviewModel()
        async with review_world(tool_env, model) as world:
            value = draft(await draft_item(world))

            async def cancel(request):
                raise asyncio.CancelledError

            model.complete = cancel
            with pytest.raises(asyncio.CancelledError):
                await run_submit(world, value)
            assert (
                tool_env.calls.get_call(tool_env.run.id, "user-1", "submit").status == "executing"
            )
            with tool_env.database.connection() as connection:
                assert connection.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 0

    asyncio.run(run())


def test_checked_source_version_is_revalidated_for_review(tool_env):
    async def run():
        async with review_world(tool_env, ReviewModel()) as world:
            context, proposed, verified = checked(world, draft(await draft_item(world)))
            meta = verified.sources[0]
            with tool_env.database.connection() as connection:
                connection.execute(
                    "UPDATE sources SET metadata_json=? WHERE id=?",
                    (
                        meta.model_copy(update={"title": "修改标题"}).model_dump_json(),
                        meta.source_id,
                    ),
                )
            with pytest.raises(ToolFailure) as error:
                world.reviewer.input.build(context, proposed, verified)
            assert error.value.result.error.code == "SOURCE_STATE_PROBLEM"

    asyncio.run(run())


@pytest.mark.parametrize("limit", [255, 50001, 0])
def test_review_context_config_is_validated(limit):
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_review_context_chars=limit)


@pytest.mark.parametrize("change", ["first", "short", "long"])
def test_citation_feedback_locates_mismatch_without_changing_draft(brief_env, change):
    ready = brief_env
    ref = ready.draft["items"][0]["citations"][0]
    if change == "first":
        ref["quote"] = "错" + ref["quote"][1:]
        position = 0
    elif change == "short":
        ref["quote"] = ref["quote"][:-1]
        position = 49
    else:
        ref["quote"] += "!"
        position = 50
    original = deepcopy(ref)
    result = execute(ready)
    assert result.error.code == "BRIEF_RULES_REJECTED"
    message = result.error.details[0].issue
    assert f"字符{position}" in message and "定位" in message
    assert ref == original and len(message) <= 80
    assert not ready.reviews
