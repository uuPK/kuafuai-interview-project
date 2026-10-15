import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.storage.database as database_module
from app.agent.brief_rules import BriefRules
from app.agent.briefs import BriefDraft, ReviewOutcome, SubmitParameters
from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.storage.briefs import BriefStore
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.models import BudgetCharge, RunRequest
from app.storage.research import ResearchStore
from app.storage.runs import RunStore
from app.tools.context import bind_context
from app.tools.executor import ToolExecutor
from app.tools.models import ToolFailure, ToolInvocation, ToolResult
from app.tools.registry import ToolRegistry
from app.tools.submit import SubmissionTool, register_submission, submission_completion
from tests.research_support import ResearchWorld
from tests.test_fetch import Pages, fetch_call
from tests.test_fetch import install as install_fetch
from tests.test_research import seed_history


def invocation(draft, call_id="submit"):
    return ToolInvocation(
        tool_call_id=call_id,
        name="submit_brief",
        arguments=json.dumps({"draft": draft}, ensure_ascii=False),
    )


@pytest.fixture
def brief_env(tool_env):
    env = tool_env

    async def prepare():
        fetched = await install_fetch(env, Pages(env))
        result = await fetched.executor.execute(env.run.id, "user-1", fetch_call(fetched.ids[0]))
        meta, index = fetched.store.get_source(env.run.id, "user-1", result.data["source_id"])
        context = bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1")
        body = fetched.store.files.verify(context, meta, index)
        return fetched, meta, index, context, body

    fetched, meta, index, context, body = asyncio.run(prepare())
    store = BriefStore(
        ResearchStore(env.calls, env.settings), BriefRules(fetched.store, env.settings)
    )
    registry, reviews = ToolRegistry(), []

    async def fixture_review(context, draft, checked):
        reviews.append(checked)
        return ReviewOutcome(approved=True, issues=())

    register_submission(registry, store, reviewer=fixture_review, review_basis="fixture")
    draft = {
        "title": "每日AI新闻",
        "items": [
            {
                "title": "模型发布",
                "summary": "固定原文提供模型发布信息。",
                "topics": ["开源模型"],
                "event_id": "model-v1",
                "key_facts": ["模型已发布"],
                "selection_reason": "话题相关，原文可读回",
                "citations": [
                    {
                        "source_id": meta.source_id,
                        "start_offset": 0,
                        "end_offset": 50,
                        "quote": body[:50],
                    }
                ],
            }
        ],
        "uncovered_topics": [],
        "shortfall_reason": "本次材料只支持一条相关新闻",
    }
    return SimpleNamespace(
        env=env,
        store=store,
        registry=registry,
        executor=ToolExecutor(registry, env.calls, env.settings),
        meta=meta,
        index=index,
        body=body,
        draft=draft,
        fetched=fetched,
        reviews=reviews,
        context=context,
    )


def execute(ready, draft=None, call_id="submit", **kwargs):
    return asyncio.run(
        ready.executor.execute(
            ready.env.run.id,
            "user-1",
            invocation(ready.draft if draft is None else draft, call_id),
            **kwargs,
        )
    )


def counts(ready):
    with ready.env.database.connection() as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("briefs", "delivered_history", "submission_jobs")
        )


def metadata(ready, **updates):
    value = ready.meta.model_copy(update=updates)
    with ready.env.database.connection() as connection:
        connection.execute(
            "UPDATE sources SET metadata_json=? WHERE id=?",
            (value.model_dump_json(), ready.meta.source_id),
        )


def subscription(ready, **updates):
    with ready.env.database.connection() as connection:
        value = ready.env.run.subscription.model_copy(update=updates)
        connection.execute(
            "UPDATE runs SET subscription_json=? WHERE id=?",
            (value.model_dump_json(), ready.env.run.id),
        )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"title": " ", "items": []},
        {"title": "a", "items": "a"},
        {"title": "a", "items": [], "recipient": "attacker@example.com"},
        {"title": "a", "items": [], "window_end": "2030-01-01"},
        {"title": "a", "items": [], "shortfall_reason": " "},
        {"title": "a", "items": [], "uncovered_topics": [{"topic": "AI", "reason": ""}]},
        {"title": "a", "items": [{}] * 6},
    ],
)
def test_draft_rejects_invalid_shapes(fields):
    with pytest.raises(ValidationError):
        BriefDraft.model_validate_json(json.dumps(fields))


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", ""),
        ("title", " "),
        ("event_id", ""),
        ("selection_reason", "x" * 501),
        ("key_facts", []),
        ("topics", []),
        ("citations", []),
        ("url", "https://attacker.example.com"),
        ("entities", ["a"] * 11),
        ("summary", "x" * 2001),
    ],
)
def test_item_schema_is_strict(brief_env, field, value):
    ready = brief_env
    ready.draft["items"][0][field] = value
    result = execute(ready)
    assert result.error.code == "INVALID_ARGUMENTS"
    assert counts(ready) == (0, 0, 0) and not ready.reviews


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "src-" + "0" * 32),
        ("start_offset", 51),
        ("end_offset", 999999),
        ("quote", "伪造摘录"),
        ("quote", "x" * 50),
    ],
)
def test_forged_citations_return_precise_rules_feedback(brief_env, field, value):
    ready = brief_env
    ready.draft["items"][0]["citations"][0][field] = value
    result = execute(ready)
    assert result.error.code == "BRIEF_RULES_REJECTED" and result.error.details
    assert "citations[0]" in result.error.details[0].field
    assert counts(ready) == (0, 0, 0) and not ready.reviews
    assert ready.env.runs.get_run(ready.env.run.id, "user-1").status == "running"


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_offset", True),
        ("end_offset", "50"),
        ("quote", ""),
        ("source_id", "../../.env"),
        ("content_hash", "f" * 64),
        ("url", "https://example.com"),
        ("published_at", "2026-10-13T00:00:00Z"),
    ],
)
def test_citation_schema_cannot_override_server_fields(brief_env, field, value):
    ready = brief_env
    ready.draft["items"][0]["citations"][0][field] = value
    assert execute(ready).error.code == "INVALID_ARGUMENTS"
    assert counts(ready) == (0, 0, 0)


@pytest.mark.parametrize(
    "change",
    ["count", "shortfall", "unknown", "duplicate_topic", "missing", "overlap", "duplicate_missing"],
)
def test_item_count_and_topic_coverage(brief_env, change):
    ready = brief_env
    item = ready.draft["items"][0]
    if change == "count":
        subscription(ready, max_items=1)
        ready.draft["items"].append(item.copy())
    elif change == "shortfall":
        ready.draft.pop("shortfall_reason")
    elif change == "unknown":
        item["topics"] = ["AI芯片"]
    elif change == "duplicate_topic":
        item["topics"].append("开源模型")
    elif change == "missing":
        subscription(ready, topics=("开源模型", "AI芯片"))
    else:
        ready.draft["uncovered_topics"] = [{"topic": "开源模型", "reason": "没有新闻"}]
        if change == "duplicate_missing":
            ready.draft["uncovered_topics"] *= 2
    assert execute(ready).error.code == "BRIEF_RULES_REJECTED"
    assert not ready.reviews and counts(ready) == (0, 0, 0)


@pytest.mark.parametrize(
    "status,delta", [("missing", 0), ("conflict", 0), ("confirmed", -25), ("confirmed", 1)]
)
def test_source_date_must_be_confirmed_inside_window(brief_env, status, delta):
    ready = brief_env
    metadata(
        ready,
        publication=ready.meta.publication.model_copy(
            update={
                "status": status,
                "published_at": ready.env.run.window_end + timedelta(hours=delta),
            }
        ),
    )
    result = execute(ready)
    assert result.error.code == "BRIEF_RULES_REJECTED"
    assert any("窗口" in i.issue for i in result.error.details) and counts(ready) == (0, 0, 0)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_body",
        "body_hash",
        "missing_index",
        "index_hash",
        "metadata_run",
        "metadata_id",
        "metadata_url",
        "row_hash",
        "candidate",
    ],
)
def test_source_corruption_never_reaches_review_or_delivery(brief_env, corruption):
    ready, env = brief_env, brief_env.env
    if corruption in {"missing_body", "body_hash", "missing_index", "index_hash"}:
        path = (
            env.settings.data_dir
            / "runs"
            / env.run.id
            / (ready.meta.file_path if "body" in corruption else ready.meta.index_path)
        )
        if corruption.startswith("missing"):
            path.unlink()
        else:
            path.write_bytes(path.read_bytes() + b"x")
    elif corruption == "row_hash":
        with env.database.connection() as connection:
            connection.execute(
                "UPDATE sources SET content_hash=? WHERE id=?", ("0" * 64, ready.meta.source_id)
            )
    else:
        field = {
            "metadata_run": "run_id",
            "metadata_id": "source_id",
            "metadata_url": "url",
            "candidate": "candidate_id",
        }[corruption]
        metadata(ready, **{field: "src-" + "0" * 32 if field == "source_id" else "foreign"})
    assert execute(ready).error.code == "SOURCE_STATE_PROBLEM"
    assert counts(ready) == (0, 0, 0) and not ready.reviews


@pytest.mark.parametrize(
    "where", ["title", "summary", "selection_reason", "key_facts", "source_title", "source_body"]
)
def test_exclusions_apply_to_draft_and_fixed_source(brief_env, where):
    ready = brief_env
    word = ready.body[80:90] if where == "source_body" else "BLOCKED"
    subscription(ready, exclude_keywords=(word.lower(),))
    if where == "source_title":
        metadata(ready, title="BLOCKED")
    elif where in {"title", "summary", "selection_reason"}:
        ready.draft["items"][0][where] = "BLOCKED"
    elif where == "key_facts":
        ready.draft["items"][0][where] = ["BLOCKED"]
    assert execute(ready).error.code == "BRIEF_RULES_REJECTED" and not ready.reviews


def test_valid_brief_atomic_history_user_isolation_and_repeat(brief_env):
    ready, env = brief_env, brief_env.env
    result = execute(ready)
    assert result.ok and result.data["review_basis"] == "fixture" and counts(ready) == (1, 1, 1)
    assert env.runs.get_run(env.run.id, "user-1").status == "completed"
    assert execute(ready) == result and len(ready.reviews) == 1
    saved = ready.store.get(result.data["brief_id"], "user-1")
    citation = saved["document"].items[0].citations[0]
    assert citation.url == ready.meta.url and citation.content_hash == ready.meta.content_hash
    assert citation.published_at == ready.meta.publication.published_at
    with pytest.raises(LookupError):
        ready.store.get(result.data["brief_id"], "other")
    next_run = env.runs.create_run(
        RunRequest(user_id="user-1", subscription_id="next", subscription=env.run.subscription)
    )
    history, total = ready.store.research.history(
        bind_context(env.runs, env.settings.data_dir, next_run.id, "user-1"), 7, 10
    )
    assert total == 1 and history[0]["key_facts"] == ["模型已发布"]
    reopened = Database(env.database.path)
    reopened.initialize()
    store = BriefStore(
        ResearchStore(CallStore(RunStore(reopened, env.settings)), env.settings), ready.store.rules
    )
    assert store.get(result.data["brief_id"], "user-1") == saved


def test_production_without_independent_review_does_not_accept(brief_env):
    ready, env = brief_env, brief_env.env
    registry = ToolRegistry()
    register_submission(registry, ready.store)
    ready.executor = ToolExecutor(registry, env.calls, env.settings)
    assert execute(ready).error.code == "BRIEF_REVIEW_UNAVAILABLE"
    assert counts(ready) == (0, 0, 0)


def test_duplicate_material_is_a_clue_and_duplicate_citation_is_rejected(brief_env):
    ready = brief_env
    ready.draft["items"][0]["citations"] *= 2
    assert execute(ready, call_id="duplicate").error.code == "BRIEF_RULES_REJECTED"
    ready.draft["items"][0]["citations"] = ready.draft["items"][0]["citations"][:1]
    ready.draft["items"].append({**ready.draft["items"][0], "key_facts": ["另一独立事件线索"]})
    assert execute(ready).ok and counts(ready) == (1, 2, 1)
    assert {h["kind"] for h in ready.reviews[0].duplicate_hints} == {"same_url", "same_body"}


def interrupt_feedback(*args):
    raise RuntimeError("feedback interrupted")


def recover_result(ready):
    context = bind_context(
        ready.env.runs,
        ready.env.settings.data_dir,
        ready.env.run.id,
        "user-1",
        tool_call_id="submit",
    )
    return asyncio.run(
        ready.registry.get("submit_brief").handler(
            context, SubmitParameters.model_validate_json(invocation(ready.draft).arguments)
        )
    )


def test_feedback_rollback_and_same_intent_recovery(brief_env):
    ready, env = brief_env, brief_env.env
    with pytest.raises(RuntimeError):
        execute(ready, on_finished=interrupt_feedback)
    assert counts(ready) == (0, 0, 1) and env.runs.get_run(env.run.id, "user-1").status == "running"
    assert env.calls.get_call(env.run.id, "user-1", "submit").result is None
    result = recover_result(ready)
    ready.executor.finish(env.run.id, "user-1", invocation(ready.draft), result)
    assert counts(ready) == (1, 1, 1) and len(ready.reviews) == 1
    assert execute(ready) == result
    assert (
        len([e for e in env.runs.list_events(env.run.id, "user-1") if e.kind == "brief_saved"]) == 1
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "run_id",
        "tool_call_id",
        "invocation_hash",
        "draft",
        "sources",
        "duplicate_hints",
        "invalid_json",
    ],
)
def test_corrupt_prepared_intent_cannot_be_delivered(brief_env, corruption):
    ready = brief_env
    with pytest.raises(RuntimeError):
        execute(ready, on_finished=interrupt_feedback)
    with ready.env.database.connection() as connection:
        job = json.loads(
            connection.execute("SELECT payload_json FROM submission_jobs").fetchone()[0]
        )
        if corruption == "draft":
            job["draft"]["title"] = "changed"
        elif corruption == "sources":
            job["sources"][0]["title"] = "changed"
        elif corruption == "duplicate_hints":
            job["duplicate_hints"] = [{"kind": "same_url", "items": [0, 1]}]
        else:
            job[corruption] = "0" * 64 if corruption == "invocation_hash" else "foreign"
        connection.execute(
            "UPDATE submission_jobs SET payload_json=?",
            ("{" if corruption == "invalid_json" else json.dumps(job),),
        )
    with pytest.raises(ToolFailure) as error:
        recover_result(ready)
    assert error.value.result.error.code == "BRIEF_STATE_PROBLEM" and counts(ready) == (0, 0, 1)


def empty_draft():
    return {
        "title": "暂无符合订阅条件的新闻",
        "items": [],
        "shortfall_reason": "有效检索无符合条件的材料",
        "uncovered_topics": [{"topic": "开源模型", "reason": "有效清单零结果"}],
    }


def test_empty_brief_requires_valid_discovery(tool_env):
    async def run():
        env = tool_env
        async with ResearchWorld(env, zero_first=True) as world:
            store = BriefStore(world.research, BriefRules(world.sources, env.settings))
            registry = ToolRegistry()

            async def fixture_review(*args):
                return ReviewOutcome(approved=True, issues=())

            register_submission(registry, store, reviewer=fixture_review, review_basis="fixture")
            executor = ToolExecutor(registry, env.calls, env.settings)
            first = await executor.execute(
                env.run.id, "user-1", invocation(empty_draft(), "no-discovery")
            )
            assert first.error.code == "BRIEF_RULES_REJECTED"
            feed = await world.executor.execute(
                env.run.id,
                "user-1",
                ToolInvocation(
                    tool_call_id="feed",
                    name="read_feed",
                    arguments='{"feed_id":"fixture-official"}',
                ),
            )
            assert feed.ok and feed.data["items"] == []
            result = await executor.execute(env.run.id, "user-1", invocation(empty_draft()))
            assert result.ok and result.data["status"] == "no_news"
            assert env.runs.get_run(env.run.id, "user-1").status == "no_news"

    asyncio.run(run())


def test_existing_candidate_without_body_and_failed_fetch_cannot_be_no_news(tool_env):
    async def run():
        env = tool_env
        async with ResearchWorld(env, blocked_first=True) as world:
            feed = await world.executor.execute(
                env.run.id,
                "user-1",
                ToolInvocation(
                    tool_call_id="feed",
                    name="read_feed",
                    arguments='{"feed_id":"fixture-official"}',
                ),
            )
            rules = BriefRules(world.sources, env.settings)
            draft = BriefDraft.model_validate_json(json.dumps(empty_draft()))
            context = bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1")
            _, result = rules.check(context, draft)
            assert any("未取得正文" in i.issue for i in result.error.details)
            failed = await world.executor.execute(
                env.run.id, "user-1", fetch_call(feed.data["items"][0]["candidate_id"])
            )
            assert not failed.ok
            _, result = rules.check(context, draft)
            assert any("未成功调用" in i.issue for i in result.error.details)

    asyncio.run(run())


def test_agent_corrects_rejected_range_and_pairs_remaining_calls(brief_env, response_factory):
    ready, env = brief_env, brief_env.env
    good = json.loads(json.dumps(ready.draft))
    ready.draft["items"][0]["citations"][0]["end_offset"] -= 1
    conversations = ConversationStore(env.calls)

    def revise(request):
        assert json.loads(request.messages[-1].content)["error"]["code"] == "BRIEF_RULES_REJECTED"
        return response_factory(
            ("second", "submit_brief", {"draft": good}), ("late", "submit_brief", {"draft": good})
        )

    model = ScriptedModel(
        [response_factory(("first", "submit_brief", {"draft": ready.draft})), revise]
    )
    done = asyncio.run(
        AgentRunner(model, ready.executor, conversations, env.settings).run(env.run.id, "user-1")
    )
    assert done.status == "completed" and counts(ready) == (1, 1, 1)
    assert env.calls.get_call(env.run.id, "user-1", "late").result.error.code == "RUN_FINISHED"
    ModelRequest(
        messages=conversations.messages(env.run.id, "user-1"), tools=tuple(ready.registry.schemas())
    )
    assert done.budget_usage.model_requests == 2 and len(ready.reviews) == 1


def test_permission_sensitive_content_and_small_feedback(brief_env):
    ready, env = brief_env, brief_env.env
    config = env.settings.model_copy(update={"max_tool_result_chars": 256})
    ready.store.rules.settings = config
    ready.executor = ToolExecutor(ready.registry, env.calls, config)
    denied = asyncio.run(ready.executor.execute(env.run.id, "other", invocation(ready.draft)))
    assert denied.error.code == "PERMISSION_DENIED"
    ready.draft["items"][0]["summary"] += " fixture-deep-key"
    result = execute(ready)
    assert result.error.code == "BRIEF_RULES_REJECTED" and len(result.model_dump_json()) <= 256
    assert "fixture-deep-key" not in result.model_dump_json() and counts(ready) == (0, 0, 0)
    with pytest.raises(PermissionError):
        asyncio.run(
            SubmissionTool(ready.store).submit(
                ready.context,
                SubmitParameters.model_validate_json(invocation(ready.draft).arguments),
            )
        )
    assert submission_completion(ToolResult.success(None)) is None


def test_v9_migration_rolls_back_and_is_idempotent(tmp_path, monkeypatch):
    db = Database(tmp_path / "migration.sqlite3")
    schema = database_module.SCHEMA_V9
    worker_schema = database_module.SCHEMA_V11
    review_schema = database_module.SCHEMA_V10
    monkeypatch.setattr(database_module, "SCHEMA_V11", ())
    monkeypatch.setattr(database_module, "SCHEMA_V10", ())
    monkeypatch.setattr(database_module, "SCHEMA_V9", ())
    db.initialize()
    with db.connection() as connection:
        connection.execute("PRAGMA user_version=8")
    monkeypatch.setattr(database_module, "SCHEMA_V9", (*schema, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        db.initialize()
    with db.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='briefs'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V9", schema)
    monkeypatch.setattr(database_module, "SCHEMA_V10", review_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V11", worker_schema)
    db.initialize()
    db.initialize()
    with db.connection() as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == database_module.SCHEMA_VERSION
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"approved": True, "issues": [{"field": "summary", "issue": "bad"}]},
        {"approved": False, "issues": []},
    ],
)
def test_malformed_review_outcome_cannot_accept(brief_env, response):
    ready, env = brief_env, brief_env.env

    async def malformed(*args):
        return response

    registry = ToolRegistry()
    register_submission(registry, ready.store, reviewer=malformed, review_basis="fixture")
    ready.executor = ToolExecutor(registry, env.calls, env.settings)
    assert execute(ready).error.code == "INTERNAL_TOOL_ERROR"
    assert counts(ready) == (0, 0, 0)


def test_review_rejection_is_feedback_and_not_a_saved_brief(brief_env):
    ready, env = brief_env, brief_env.env

    async def rejected(*args):
        from app.tools.models import ArgumentIssue

        return ReviewOutcome(
            approved=False,
            issues=(ArgumentIssue(field="draft.items[0].summary", issue="核对替身指出缺少条件"),),
        )

    registry = ToolRegistry()
    register_submission(registry, ready.store, reviewer=rejected, review_basis="fixture")
    ready.executor = ToolExecutor(registry, env.calls, env.settings)
    result = execute(ready)
    assert result.error.code == "BRIEF_REVIEW_REJECTED" and result.error.details[0].issue.endswith(
        "条件"
    )
    assert counts(ready) == (0, 0, 0)


def test_source_change_between_check_and_commit_rolls_back(brief_env):
    ready, env = brief_env, brief_env.env

    async def mutate_after_review(*args):
        path = env.settings.data_dir / "runs" / env.run.id / ready.meta.file_path
        path.write_bytes(path.read_bytes() + b"x")
        return ReviewOutcome(approved=True, issues=())

    registry = ToolRegistry()
    register_submission(registry, ready.store, reviewer=mutate_after_review, review_basis="fixture")
    ready.executor = ToolExecutor(registry, env.calls, env.settings)
    with pytest.raises(ToolFailure) as error:
        execute(ready)
    assert error.value.result.error.code == "SOURCE_STATE_PROBLEM"
    assert counts(ready) == (0, 0, 1) and env.runs.get_run(env.run.id, "user-1").status == "running"
    assert env.calls.get_call(env.run.id, "user-1", "submit").result is None


def test_evidence_total_limit_and_exact_newlines(brief_env):
    ready, env = brief_env, brief_env.env
    ref = ready.draft["items"][0]["citations"][0]
    ref["end_offset"] = len(ref["quote"].encode("utf-8"))
    assert execute(ready, call_id="byte-offset").error.code == "BRIEF_RULES_REJECTED"
    ref["end_offset"] = 50
    ready.store.rules.settings = env.settings.model_copy(update={"max_material_scan_bytes": 1})
    assert execute(ready).error.code == "BRIEF_EVIDENCE_TOO_LARGE"
    assert counts(ready) == (0, 0, 0)


def test_historical_source_cannot_replace_current_news_evidence(brief_env):
    ready = brief_env
    historical = asyncio.run(seed_history(ready.env))
    ready.draft["items"][0]["citations"][0] = historical.event.citations[0].model_dump()
    result = execute(ready)
    assert result.error.code == "BRIEF_RULES_REJECTED"
    assert any("当前任务" in issue.issue for issue in result.error.details)
    assert counts(ready) == (0, 1, 0)


def test_history_write_failure_rolls_back_entire_delivery(brief_env, monkeypatch):
    ready, env = brief_env, brief_env.env
    original = ready.store.research.record_delivery

    def interrupted_history(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("history write interrupted")

    monkeypatch.setattr(ready.store.research, "record_delivery", interrupted_history)
    with pytest.raises(RuntimeError, match="history write"):
        execute(ready)
    assert counts(ready) == (0, 0, 1)
    assert env.runs.get_run(env.run.id, "user-1").status == "running"
    assert env.calls.get_call(env.run.id, "user-1", "submit").result is None


def test_fake_success_without_checked_intent_cannot_complete(brief_env):
    ready, env = brief_env, brief_env.env
    call = invocation(ready.draft)
    env.calls.prepare_calls(env.run.id, "user-1", [call])
    env.calls.start_call(env.run.id, "user-1", call.tool_call_id, BudgetCharge(tool_calls=1))
    with pytest.raises(ToolFailure) as error:
        ready.executor.finish(env.run.id, "user-1", call, ToolResult.success({"accepted": True}))
    assert error.value.result.error.code == "BRIEF_STATE_PROBLEM"
    assert counts(ready) == (0, 0, 0) and env.runs.get_run(env.run.id, "user-1").status == "running"


def test_altered_confirmation_cannot_commit_prepared_delivery(brief_env):
    ready, env = brief_env, brief_env.env
    with pytest.raises(RuntimeError):
        execute(ready, on_finished=interrupt_feedback)
    result = recover_result(ready)
    altered = ToolResult.success({**result.data, "brief_id": "brief-" + "0" * 32})
    with pytest.raises(ToolFailure) as error:
        ready.executor.finish(env.run.id, "user-1", invocation(ready.draft), altered)
    assert error.value.result.error.code == "BRIEF_STATE_PROBLEM" and counts(ready) == (0, 0, 1)


def test_staging_reuses_identity_and_cannot_change_review_basis(brief_env):
    ready, env = brief_env, brief_env.env
    with pytest.raises(RuntimeError):
        execute(ready, on_finished=interrupt_feedback)
    result = recover_result(ready)
    context = bind_context(
        env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="submit"
    )
    draft = BriefDraft.model_validate_json(json.dumps(ready.draft))
    assert ready.store.stage(context, draft, ready.reviews[0], "fixture") == result
    with pytest.raises(ToolFailure):
        ready.store.stage(context, draft, ready.reviews[0], "independent")
    wrong_call = bind_context(
        env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="fetch"
    )
    with pytest.raises(PermissionError):
        ready.store.stage(wrong_call, draft, ready.reviews[0], "fixture")


def test_corrupt_saved_document_returns_state_problem(brief_env):
    ready = brief_env
    result = execute(ready)
    with ready.env.database.connection() as connection:
        connection.execute("UPDATE briefs SET document_json='{}'")
    with pytest.raises(ToolFailure) as error:
        ready.store.get(result.data["brief_id"], "user-1")
    assert error.value.result.error.code == "BRIEF_STATE_PROBLEM"


@pytest.mark.parametrize(
    "saved",
    [
        "{}",
        '{"ok":true,"data":null,"error":null}',
        '{"ok":true,"data":{"items":"bad"},"error":null}',
    ],
)
def test_empty_brief_does_not_hide_corrupt_discovery(tool_env, saved):
    async def run():
        env = tool_env
        async with ResearchWorld(env, zero_first=True) as world:
            await world.executor.execute(
                env.run.id,
                "user-1",
                ToolInvocation(
                    tool_call_id="feed",
                    name="read_feed",
                    arguments='{"feed_id":"fixture-official"}',
                ),
            )
            with env.database.connection() as connection:
                connection.execute("UPDATE discoveries SET result_json=?", (saved,))
            with pytest.raises(ToolFailure) as error:
                BriefRules(world.sources, env.settings).check(
                    bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1"),
                    BriefDraft.model_validate_json(json.dumps(empty_draft())),
                )
            assert error.value.result.error.code == "DISCOVERY_STATE_PROBLEM"

    asyncio.run(run())


def test_v9_upgrade_preserves_sources_history_and_progress(brief_env):
    ready, env = brief_env, brief_env.env
    historical = asyncio.run(seed_history(env))
    with env.database.transaction() as connection:
        connection.execute(
            "INSERT INTO run_progress VALUES (?, ?, ?, ?)",
            (
                env.run.id,
                "fetch",
                '{"collected":["开源模型"],"missing":[],"issues":[]}',
                env.run.updated_at.isoformat(),
            ),
        )
        connection.execute("DROP TABLE run_leases")
        connection.execute("DROP TABLE worker_instances")
        connection.execute("DROP TABLE brief_reviews")
        connection.execute("DROP TABLE briefs")
        connection.execute("DROP TABLE submission_jobs")
        connection.execute("PRAGMA user_version=8")
    before = env.runs.get_run(env.run.id, "user-1")
    env.database.initialize()
    assert env.runs.get_run(env.run.id, "user-1") == before
    assert (
        ready.store.rules.sources.get_source(env.run.id, "user-1", ready.meta.source_id)[0]
        == ready.meta
    )
    assert ready.store.research.progress(env.run.id, "user-1").collected == ("开源模型",)
    with env.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM delivered_history WHERE run_id=?", (historical.run.id,)
            ).fetchone()[0]
            == 1
        )
