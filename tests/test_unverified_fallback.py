import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent.briefs import BriefDocument, SubmitParameters, UnverifiedItem
from app.agent.protocol import AssistantMessage, ModelFailure, ModelResponse
from app.agent.unverified import unverified_draft
from app.tools.context import bind_context
from app.tools.models import ArgumentIssue, ToolFailure
from tests.brief_support import MODEL_SUMMARY, draft, draft_item, review_world
from tests.test_briefs import interrupt_feedback, invocation
from tests.test_delivery import scheduled
from tests.test_research import seed_history
from tests.test_review import ReviewModel, reject


def counts(env):
    with env.database.connection() as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("briefs", "delivered_history", "mail_outbox")
        )


@pytest.mark.parametrize("first_failure", ["review", "quote"])
def test_last_negative_review_delivers_only_original_leads(tool_env, first_failure):
    async def run():
        env = tool_env
        model = ReviewModel(reject(), reject())
        async with review_world(env, model) as world:
            value = draft(await draft_item(world))
            first = deepcopy(value)
            if first_failure == "quote":
                first["items"][0]["citations"][0]["quote"] = "[fixture] bad quote"
            rejected = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(first, "a")
            )
            assert not rejected.ok and counts(env) == (0, 0, 0)
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            assert result.ok and result.data["review_status"] == "unverified"
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            assert not document.items and document.review_status == "unverified"
            source, _ = world.sources.get_source(
                env.run.id, env.run.user_id, value["items"][0]["citations"][0]["source_id"]
            )
            assert document.unverified_items[0].url == source.url
            assert "23%" in document.unverified_items[0].reason
            assert document.unverified_items[0].summary == MODEL_SUMMARY
            assert document.unverified_items[0].topics == ("开源模型",)
            assert "简介尚未核实" in document.unverified_items[0].reason
            assert counts(env) == (1, 0, 0)
            assert len(model.requests) == (2 if first_failure == "review" else 1)
            assert env.runs.get_run(env.run.id, env.run.user_id).status == "completed"

    asyncio.run(run())


def test_final_negative_review_mail_labels_retained_summary_as_unverified(tool_env):
    async def run():
        env = tool_env
        async with review_world(env, ReviewModel(reject(), reject())) as world:
            value = draft(await draft_item(world))
            scheduled(SimpleNamespace(env=env))
            await world.executor.execute(env.run.id, env.run.user_id, invocation(value, "a"))
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            mail = world.briefs.outbox.get(result.data["brief_id"], env.run.user_id)
            assert "最终事实核对仍有问题" in mail.body_text
            assert "原文：https://" in mail.body_text and "核对问题" in mail.body_text
            assert MODEL_SUMMARY in mail.body_text and "简介尚未核实：" in mail.body_text
            assert mail.status == "pending" and counts(env) == (1, 0, 1)

    asyncio.run(run())


def test_pre_upgrade_fallback_recovers_original_intent_and_enriches_reading_only(tool_env):
    async def run():
        env = tool_env
        async with review_world(env, ReviewModel(reject(), reject())) as world:
            value = draft(await draft_item(world))
            scheduled(SimpleNamespace(env=env))
            await world.executor.execute(env.run.id, env.run.user_id, invocation(value, "a"))
            call = invocation(value, "b")
            with pytest.raises(RuntimeError):
                await world.executor.execute(
                    env.run.id, env.run.user_id, call, on_finished=interrupt_feedback
                )
            with env.database.transaction() as connection:
                row = connection.execute("SELECT payload_json FROM submission_jobs").fetchone()
                payload = json.loads(row[0])
                payload.pop("version")
                pending = payload["fallback_document"]["unverified_items"][0]
                pending.pop("summary")
                pending.pop("topics")
                pending["reason"] = pending["reason"].replace(
                    "最终事实核对仍有问题，以下简介尚未核实。",
                    "最终事实核对仍有问题，本条仅保留原文线索。",
                )
                connection.execute(
                    "UPDATE submission_jobs SET payload_json=?", (json.dumps(payload),)
                )
            context = bind_context(
                env.runs, env.settings.data_dir, env.run.id, env.run.user_id, tool_call_id="b"
            )
            result = await world.registry.get("submit_brief").handler(
                context, SubmitParameters.model_validate_json(call.arguments)
            )
            world.executor.finish(env.run.id, env.run.user_id, call, result)
            with env.database.connection() as connection:
                saved = connection.execute("SELECT document_json FROM briefs").fetchone()[0]
            assert MODEL_SUMMARY not in saved
            brief_id = result.data["brief_id"]
            assert (
                world.briefs.get(brief_id, env.run.user_id)["document"].unverified_items[0].summary
                == MODEL_SUMMARY
            )
            assert MODEL_SUMMARY not in world.briefs.outbox.get(brief_id, env.run.user_id).body_text
            assert counts(env) == (1, 0, 1)

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage", [None, "document_link", "review_response", "source_body", "issues"]
)
def test_fallback_intent_recovers_once_and_rejects_changed_evidence(tool_env, damage):
    async def run():
        env, model = tool_env, ReviewModel(reject(), reject())
        async with review_world(env, model) as world:
            value = draft(await draft_item(world))
            scheduled(SimpleNamespace(env=env))
            await world.executor.execute(env.run.id, env.run.user_id, invocation(value, "a"))
            call = invocation(value, "b")
            with pytest.raises(RuntimeError, match="interrupted"):
                await world.executor.execute(
                    env.run.id, env.run.user_id, call, on_finished=interrupt_feedback
                )
            assert counts(env) == (0, 0, 0)
            with env.database.connection() as connection:
                payload = json.loads(
                    connection.execute("SELECT payload_json FROM submission_jobs").fetchone()[0]
                )
            original_id = payload["brief_id"]
            if damage == "source_body":
                source = payload["sources"][0]
                path = env.settings.data_dir / "runs" / env.run.id / source["file_path"]
                path.write_bytes(path.read_bytes() + b"changed")
            elif damage:
                with env.database.transaction() as connection:
                    if damage == "review_response":
                        response = ModelResponse(
                            message=AssistantMessage(content='{"approved":true,"issues":[]}')
                        )
                        connection.execute(
                            "UPDATE brief_reviews SET response_json=? WHERE tool_call_id='b'",
                            (response.model_dump_json(),),
                        )
                    else:
                        if damage == "document_link":
                            payload["fallback_document"]["unverified_items"][0]["url"] = (
                                "https://changed.example.com"
                            )
                        else:
                            payload["review_issues"][0]["issue"] = "changed"
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
            assert result.data["brief_id"] == original_id
            assert await world.executor.execute(env.run.id, env.run.user_id, call) == result
            assert counts(env) == (1, 0, 1) and len(model.requests) == 2
            assert (
                world.briefs.get(original_id, env.run.user_id)["document"].review_status
                == "unverified"
            )

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["authentication", "protocol", "service", "input"])
def test_last_service_protocol_and_input_failures_do_not_become_leads(tool_env, failure):
    async def run():
        env = tool_env
        second = {
            "authentication": ModelFailure("AUTH", category="authentication"),
            "protocol": ModelResponse(message=AssistantMessage(content="{}")),
            "service": ModelFailure("DOWN"),
            "input": reject(),
        }[failure]
        model = ReviewModel(reject(), second)
        async with review_world(env, model) as world:
            value = draft(await draft_item(world))
            await world.executor.execute(env.run.id, env.run.user_id, invocation(value, "a"))
            if failure == "input":
                value["items"][0]["summary"] = "English only summary."
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            assert not result.ok and counts(env) == (0, 0, 0)
            assert env.runs.get_run(env.run.id, env.run.user_id).status == "failed"

    asyncio.run(run())


def test_fallback_keeps_at_most_five_unique_candidates_and_marks_all_main_unconfirmed(tool_env):
    async def run():
        env = tool_env
        async with review_world(env, ReviewModel()) as world:
            item = await draft_item(world)
            proposed = SubmitParameters.model_validate_json(invocation(draft(item)).arguments).draft
            sources, items = [], []
            for i in range(5):
                source_id = "src-" + f"{i:032x}"
                sources.append(
                    SimpleNamespace(source_id=source_id, candidate_id="cand-" + f"{i:032x}")
                )
                citation = (
                    proposed.items[0].citations[0].model_copy(update={"source_id": source_id})
                )
                items.append(proposed.items[0].model_copy(update={"citations": (citation,)}))
            original = proposed.model_copy(
                update={
                    "items": tuple(items),
                    "unverified_items": (
                        UnverifiedItem(candidate_id="cand-" + f"{5:032x}", reason="已有待核实候选"),
                        UnverifiedItem(candidate_id=sources[0].candidate_id, reason="重复候选"),
                    ),
                }
            )
            issues = (ArgumentIssue(field="draft.items[0].summary", issue="数值仍待核实"),)
            result = unverified_draft(
                original, SimpleNamespace(sources=sources), issues, env.run.subscription.topics
            )
            assert not result.items and len(result.unverified_items) == 5
            assert "数值仍待核实" in result.unverified_items[0].reason
            assert "整份稿件" in result.unverified_items[1].reason
            assert "前5个" in result.shortfall_reason
            large_issues = tuple(
                ArgumentIssue(field="draft.title", issue=f"核对问题{i}" + "请重新检查" * 10)
                for i in range(10)
            )
            long = unverified_draft(
                original,
                SimpleNamespace(sources=sources),
                large_issues,
                env.run.subscription.topics,
            )
            assert all(len(item.reason) <= 500 for item in long.unverified_items)
            assert "完整问题见任务核对反馈" in long.unverified_items[0].reason

    asyncio.run(run())


def test_exact_owned_history_rejection_can_deliver_leads_without_model_calls(tool_env):
    async def run():
        from tests.brief_support import MODEL_FACTS

        env, model = tool_env, ReviewModel()
        async with review_world(env, model) as world:
            value = draft(await draft_item(world))
            await seed_history(
                env, user=env.run.user_id, facts=MODEL_FACTS, event_id="atlas-v2", version="v2"
            )
            before = counts(env)
            first = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "a")
            )
            assert first.error.code == "BRIEF_REVIEW_REJECTED"
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            assert result.ok and result.data["review_status"] == "unverified"
            after = counts(env)
            assert after == (before[0] + 1, before[1], before[2]) and not model.requests
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            assert "关键事实已全部交付" in document.unverified_items[0].reason

    asyncio.run(run())


def test_unverified_document_cannot_contain_main_news(tool_env):
    async def run():
        env = tool_env
        async with review_world(env, ReviewModel()) as world:
            value = draft(await draft_item(world))
            result = await world.executor.execute(env.run.id, env.run.user_id, invocation(value))
            document = world.briefs.get(result.data["brief_id"], env.run.user_id)["document"]
            with pytest.raises(ValueError, match="original-source leads"):
                BriefDocument(**{**document.model_dump(), "review_status": "unverified"})

    asyncio.run(run())
