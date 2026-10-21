"""Per-item decisions, source attribution, immutable anchors and durable recovery."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent.briefs import BriefDraft, ReviewOutcome, SubmitParameters
from app.agent.protocol import AssistantMessage, ModelResponse
from app.agent.review import parse_review
from app.agent.unverified import retained_indices, unverified_draft
from app.tools.context import bind_context
from app.tools.models import ToolFailure
from tests.brief_support import checked, draft, draft_item, review_world
from tests.test_briefs import interrupt_feedback, invocation
from tests.test_delivery import scheduled
from tests.test_research import seed_history
from tests.test_review import Interrupted, ReviewModel, reject
from tests.test_unverified_fallback import counts


async def two_items(world):
    first = await draft_item(world)
    second = await draft_item(world, "m1")
    second.update(
        title="Atlas v1 权重开放下载",
        summary="原文报道 Atlas v1 权重已开放下载。",
        event_id="atlas-v1",
        product_version="v1",
        key_facts=["Atlas v1 权重已开放下载"],
        selection_reason="开放下载的信息有助于模型选型",
    )
    return draft(first, second)


def mixed_outcome():
    return {
        **reject(),
        "item_reviews": [
            {"item_index": 0, "approved": False, "notes": []},
            {"item_index": 1, "approved": True, "notes": ["依据单篇原文报道"]},
        ],
    }


@pytest.mark.parametrize("damage", [None, "decision", "notes", "source", "pending"])
def test_partial_delivery_recovers_once_and_only_retained_news_enter_history(tool_env, damage):
    async def scenario():
        env, model = tool_env, ReviewModel(mixed_outcome(), mixed_outcome())
        async with review_world(env, model) as world:
            value = await two_items(world)
            scheduled(SimpleNamespace(env=env))
            first = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "a")
            )
            assert first.error.code == "BRIEF_REVIEW_REJECTED"
            call = invocation(value, "b")
            with pytest.raises(RuntimeError, match="interrupted"):
                await world.executor.execute(
                    env.run.id, env.run.user_id, call, on_finished=interrupt_feedback
                )
            assert counts(env) == (0, 0, 0)
            if damage:
                with env.database.transaction() as connection:
                    payload = json.loads(
                        connection.execute("SELECT payload_json FROM submission_jobs").fetchone()[0]
                    )
                    if damage == "decision":
                        payload["review_outcome"]["item_reviews"][0]["approved"] = True
                    elif damage == "notes":
                        payload["fallback_document"]["items"][0]["verification_notes"] = [
                            "伪造说明"
                        ]
                    elif damage == "source":
                        source = payload["sources"][0]
                        path = env.settings.data_dir / "runs" / env.run.id / source["file_path"]
                        path.write_bytes(path.read_bytes() + b"changed")
                    else:
                        payload["fallback_document"]["unverified_items"][0]["summary"] = "伪造简介"
                    connection.execute(
                        "UPDATE submission_jobs SET payload_json=?", (json.dumps(payload),)
                    )
            context = bind_context(
                env.runs, env.settings.data_dir, env.run.id, env.run.user_id, tool_call_id="b"
            )
            parameters = SubmitParameters.model_validate_json(call.arguments)
            if damage:
                with pytest.raises(ToolFailure):
                    await world.registry.get("submit_brief").handler(context, parameters)
                assert counts(env) == (0, 0, 0)
                return
            result = await world.registry.get("submit_brief").handler(context, parameters)
            world.executor.finish(env.run.id, env.run.user_id, call, result)
            assert result.data["review_status"] == "partial"
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            assert [item.event_id for item in document.items] == ["atlas-v1"]
            assert document.items[0].verification_notes == ("依据单篇原文报道",)
            assert (
                len(document.unverified_items) == 1 and "23%" in document.unverified_items[0].reason
            )
            assert document.unverified_items[0].summary == value["items"][0]["summary"]
            assert not document.uncovered_topics
            with env.database.connection() as connection:
                events = connection.execute("SELECT event_json FROM delivered_history").fetchall()
            assert len(events) == 1 and json.loads(events[0][0])["event_id"] == "atlas-v1"
            mail = world.briefs.outbox.get(result.data["brief_id"], env.run.user_id)
            assert "已保留通过原文核对" in mail.body_text and "依据单篇原文报道" in mail.body_text
            assert document.unverified_items[0].url in mail.body_text
            assert await world.executor.execute(env.run.id, env.run.user_id, call) == result
            assert counts(env) == (1, 1, 1) and len(model.requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "decisions",
    [
        None,
        [],
        [{"item_index": 0, "approved": False}],
        [{"item_index": 1, "approved": True}],
        [{"item_index": 0, "approved": True}, {"item_index": 0, "approved": True}],
    ],
)
def test_new_protocol_rejects_missing_incomplete_or_contradictory_item_decisions(decisions):
    value = BriefDraft.model_validate_json(
        json.dumps(
            draft(
                {
                    "title": "新闻",
                    "summary": "来源报道",
                    "topics": ["开源模型"],
                    "event_id": "atlas",
                    "selection_reason": "有助于选型",
                    "key_facts": ["模型发布"],
                    "citations": [
                        {
                            "source_id": "src-" + "0" * 32,
                            "start_offset": 0,
                            "end_offset": 1,
                            "quote": "字",
                        }
                    ],
                }
            )
        )
    )
    raw = {"approved": True, "issues": []}
    if decisions is not None:
        raw["item_reviews"] = decisions
    with pytest.raises(ToolFailure) as error:
        parse_review(
            ModelResponse(message=AssistantMessage(content=json.dumps(raw))),
            value,
            require_items=True,
        )
    assert error.value.result.error.code == "BRIEF_REVIEW_PROTOCOL"


def test_attributed_vendor_claim_and_canonical_anchor_share_one_review(tool_env):
    async def scenario():
        env = tool_env
        note = "厂商披露，尚未独立复现"
        model = ReviewModel(
            {
                "approved": True,
                "issues": [],
                "item_reviews": [{"item_index": 0, "approved": True, "notes": [note]}],
            }
        )
        async with review_world(env, model) as world:
            item = await draft_item(world)
            item["summary"] = "厂商称，" + item["summary"]
            ref = item["citations"][0]
            correct_end = ref["end_offset"]
            ref["end_offset"] += 10000
            original = deepcopy(ref)
            scheduled(SimpleNamespace(env=env))
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(draft(item))
            )
            assert result.ok and len(model.requests) == 1
            packet = json.loads(model.requests[0].messages[1].content)
            assert packet["draft"]["items"][0]["citations"][0]["end_offset"] == correct_end
            assert ref == original
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            assert note in document.items[0].verification_notes
            assert (
                note in world.briefs.outbox.get(result.data["brief_id"], env.run.user_id).body_text
            )
            assert "单篇可读正文" in model.requests[0].messages[0].content

    asyncio.run(scenario())


@pytest.mark.parametrize("global_problem", [False, True])
def test_no_implicit_item_approval_and_global_problem_preserve_pending_path(
    tool_env, global_problem
):
    async def scenario():
        env = tool_env
        outcome = mixed_outcome()
        if global_problem:
            outcome["issues"] = [{"field": "draft.title", "issue": "标题含正文无法支持的确定结论"}]
            for item in outcome["item_reviews"]:
                item["approved"] = True
        else:
            outcome["issues"].append(
                {"field": "draft.items[1].summary", "issue": "正文无法支持核心结论"}
            )
            outcome["item_reviews"][1]["approved"] = False
        async with review_world(env, ReviewModel(outcome, outcome)) as world:
            value = await two_items(world)
            first = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "a")
            )
            assert not first.ok, first
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            assert result.ok and result.data["review_status"] == "unverified"
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            assert not document.items and len(document.unverified_items) == 2
            assert counts(env) == (1, 0, 0)

    asyncio.run(scenario())


def test_subset_history_duplicate_cannot_be_overridden_by_model(tool_env):
    async def scenario():
        env, model = (
            tool_env,
            ReviewModel({"approved": True, "issues": []}, {"approved": True, "issues": []}),
        )
        async with review_world(env, model) as world:
            value = await two_items(world)
            await seed_history(
                env,
                event_id="atlas-v2",
                version="v2",
                facts=tuple(value["items"][0]["key_facts"]),
                brief_id="old",
            )
            await world.executor.execute(env.run.id, env.run.user_id, invocation(value, "a"))
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            assert result.ok and result.data["review_status"] == "partial"
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            assert [item.event_id for item in document.items] == ["atlas-v1"]
            assert "关键事实已全部交付" in document.unverified_items[0].reason
            assert len(json.loads(model.requests[-1].messages[1].content)["mandatory_issues"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("approved", [True, False])
def test_legacy_saved_review_resumes_original_protocol_without_model_charge(
    tool_env, monkeypatch, approved
):
    async def scenario():
        env, model = (
            tool_env,
            ReviewModel({"approved": True, "issues": []} if approved else reject()),
        )
        async with review_world(env, model) as world:
            value = draft(await draft_item(world))
            if not approved:
                bad = deepcopy(value)
                bad["items"][0]["citations"][0]["quote"] = "不存在的引文"
                await world.executor.execute(env.run.id, env.run.user_id, invocation(bad, "a"))
            build, stage = world.reviewer.input.build, world.briefs.stage
            monkeypatch.setattr(
                world.reviewer.input, "build", lambda *args, **kwargs: build(*args, protocol=1)
            )
            monkeypatch.setattr(
                world.briefs, "stage", lambda *args, **kwargs: (_ for _ in ()).throw(Interrupted())
            )
            with pytest.raises(Interrupted):
                await world.executor.execute(env.run.id, env.run.user_id, invocation(value))
            monkeypatch.setattr(world.reviewer.input, "build", build)
            monkeypatch.setattr(world.briefs, "stage", stage)
            context = bind_context(
                env.runs, env.settings.data_dir, env.run.id, env.run.user_id, tool_call_id="submit"
            )
            result = await world.registry.get("submit_brief").handler(
                context, SubmitParameters.model_validate_json(invocation(value).arguments)
            )
            world.executor.finish(env.run.id, env.run.user_id, invocation(value), result)
            assert result.ok and len(model.requests) == 1
            assert "review_protocol" not in json.loads(model.requests[0].messages[1].content)
            assert counts(env) == ((1, 1, 0) if approved else (1, 0, 0))

    asyncio.run(scenario())


def test_nonunique_unchanged_quote_is_not_automatically_relocated(tool_env):
    async def scenario():
        async with review_world(tool_env, ReviewModel()) as world:
            value = draft(await draft_item(world))
            _, _, verified = checked(world, value)
            ref = value["items"][0]["citations"][0]
            body = (
                tool_env.settings.data_dir
                / "runs"
                / tool_env.run.id
                / verified.sources[0].file_path
            ).read_text(encoding="utf-8")
            assert body.count(" ") > 1
            ref.update(quote=" ", start_offset=0, end_offset=1)
            result = await world.executor.execute(
                tool_env.run.id, tool_env.run.user_id, invocation(value)
            )
            assert result.error.code == "BRIEF_RULES_REJECTED" and not world.reviewer.model.requests

    asyncio.run(scenario())


def test_nested_parameter_feedback_identifies_object_without_echoing_unknown_input(tool_env):
    async def scenario():
        async with review_world(tool_env, ReviewModel()) as world:
            value = draft(await draft_item(world))
            value["items"][0]["citations"][0]["private-fixture-password"] = "secret-fixture-value"
            result = await world.executor.execute(
                tool_env.run.id, tool_env.run.user_id, invocation(value)
            )
            assert result.error.code == "INVALID_ARGUMENTS"
            assert result.error.details[0].field == "draft.items[0].citations[0].[unknown]"
            assert "private-fixture-password" not in result.model_dump_json()
            assert "secret-fixture-value" not in result.model_dump_json()
            assert "删除" in result.error.message

    asyncio.run(scenario())


def test_mandatory_issue_is_preserved_when_model_returns_ten_other_issues(tool_env):
    async def scenario():
        async with review_world(tool_env, ReviewModel()) as world:
            value = BriefDraft.model_validate_json(json.dumps(await two_items(world)))
            raw = {
                "approved": False,
                "issues": [
                    {"field": "draft.items[1].summary", "issue": f"待修正问题{i}"}
                    for i in range(10)
                ],
                "item_reviews": [
                    {"item_index": 0, "approved": True},
                    {"item_index": 1, "approved": False},
                ],
            }
            outcome = parse_review(
                ModelResponse(message=AssistantMessage(content=json.dumps(raw))),
                value,
                require_items=True,
                mandatory_issues=[{"field": "draft.items[0].key_facts", "issue": "确定的重复事实"}],
            )
            assert len(outcome.issues) == 10
            assert {issue.field for issue in outcome.issues} == {
                "draft.items[0].key_facts",
                "draft.items[1].summary",
            }
            assert all(not item.approved for item in outcome.item_reviews)

    asyncio.run(scenario())


def test_shared_candidate_cannot_be_both_retained_and_pending(tool_env):
    async def scenario():
        async with review_world(tool_env, ReviewModel()) as world:
            value = draft(await draft_item(world))
            value["items"].append(deepcopy(value["items"][0]))
            _, proposed, verified = checked(world, value)
            outcome = ReviewOutcome.model_validate_json(json.dumps(mixed_outcome()))
            assert retained_indices(proposed, verified, outcome) == ()

    asyncio.run(scenario())


def test_existing_pending_reason_survives_mixed_fallback(tool_env):
    async def scenario():
        async with review_world(tool_env, ReviewModel()) as world:
            context, proposed, verified = checked(world, await two_items(world))
            from app.agent.briefs import UnverifiedItem

            pending = UnverifiedItem(
                candidate_id="cand-" + "f" * 32,
                reason="已有来源的数字互相矛盾",
                summary="已有候选简介",
            )
            proposed = proposed.model_copy(update={"unverified_items": (pending,)})
            outcome = ReviewOutcome.model_validate_json(json.dumps(mixed_outcome()))
            result = unverified_draft(
                proposed,
                verified,
                outcome.issues,
                tool_env.run.subscription.topics,
                keep_indices=(1,),
                preserve_pending=True,
            )
            assert result.unverified_items[-1] == pending

    asyncio.run(scenario())
