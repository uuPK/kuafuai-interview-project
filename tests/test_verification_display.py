import asyncio
import json
from copy import deepcopy

import pytest

from app.agent.briefs import BriefDocument
from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.providers.article import Publication
from app.storage.conversations import ConversationStore
from app.tools.models import ToolFailure
from tests.brief_support import checked, draft, draft_item, review_world
from tests.test_briefs import brief_env as _brief_env
from tests.test_briefs import counts, execute, metadata, recover_result
from tests.test_delivery import scheduled
from tests.test_review import ReviewModel

brief_env = _brief_env


def pending_draft(ready):
    return {
        "title": "每日简报与待核实信息",
        "items": [],
        "uncovered_topics": [{"topic": "开源模型", "reason": "尚未形成可确认的主新闻"}],
        "shortfall_reason": "候选仍有事实待核实，未计入主新闻条数",
        "unverified_items": [
            {
                "candidate_id": ready.meta.candidate_id,
                "reason": "关键事实待核实，尚未完成交叉确认，请阅读原文",
            }
        ],
    }


def test_pending_intro_is_frozen_in_document_and_mail_without_confirmed_history(brief_env):
    ready = brief_env
    ready.draft = pending_draft(ready)
    ready.draft["unverified_items"][0].update(
        summary="报道称模型新增了工具调用能力，具体性能数字尚未核实。", topics=["开源模型"]
    )
    scheduled(ready)
    result = execute(ready)
    document = ready.store.get(result.data["brief_id"], "user-1")["document"]
    item = document.unverified_items[0]
    assert item.summary == ready.draft["unverified_items"][0]["summary"]
    assert item.topics == ("开源模型",)
    assert counts(ready) == (1, 0, 1)
    mail = ready.store.outbox.get(result.data["brief_id"], "user-1")
    assert "简介尚未核实：" + item.summary in mail.body_text


@pytest.mark.parametrize("case", ["excluded", "secret", "foreign_topic", "duplicate_topic"])
def test_pending_intro_obeys_exclusions_redaction_and_topic_boundary(brief_env, case):
    from tests.test_briefs import subscription

    ready = brief_env
    ready.draft = pending_draft(ready)
    item = ready.draft["unverified_items"][0]
    item.update(summary="这是尚未核实的简介", topics=["开源模型"])
    if case == "excluded":
        subscription(ready, exclude_keywords=("BLOCKED",))
        item["summary"] += "BLOCKED"
    elif case == "secret":
        item["summary"] += " Authorization: Bearer fixture-private-token"
    elif case == "foreign_topic":
        item["topics"] = ["未订阅话题"]
    else:
        item["topics"] *= 2
    assert execute(ready).error.code == "BRIEF_RULES_REJECTED"
    assert counts(ready) == (0, 0, 0)


def test_legacy_pending_intro_uses_owned_discovery_without_rewriting_saved_document(brief_env):
    ready = brief_env
    ready.draft = pending_draft(ready)
    result = execute(ready)
    with ready.env.database.transaction() as connection:
        value = json.loads(connection.execute("SELECT document_json FROM briefs").fetchone()[0])
        value["unverified_items"][0].pop("summary")
        value["unverified_items"][0].pop("topics")
        connection.execute("UPDATE briefs SET document_json=?", (json.dumps(value),))
    with ready.env.database.connection() as connection:
        saved = connection.execute("SELECT document_json FROM briefs").fetchone()[0]
    item = ready.store.get(result.data["brief_id"], "user-1")["document"].unverified_items[0]
    assert item.summary and "尚未核实" in item.summary
    with ready.env.database.connection() as connection:
        assert saved == connection.execute("SELECT document_json FROM briefs").fetchone()[0]


@pytest.mark.parametrize(
    "status,phrase",
    [("missing", "缺少明确发布时间"), ("ambiguous", "缺少时区"), ("conflicting", "发布时间有冲突")],
)
def test_time_gaps_publish_with_server_note_and_nullable_date(brief_env, status, phrase):
    ready = brief_env
    metadata(ready, publication=Publication(status=status))
    result = execute(ready)
    assert result.ok and result.data["status"] == "completed"
    document = ready.store.get(result.data["brief_id"], "user-1")["document"]
    assert phrase in document.items[0].verification_notes[0]
    assert document.items[0].citations[0].published_at is None
    assert document.items[0].citations[0].publication_status == status
    assert counts(ready) == (1, 1, 1)
    missing_note = document.model_dump(mode="json")
    missing_note["items"][0]["verification_notes"] = []
    with pytest.raises(ValueError, match="verification note"):
        BriefDocument.model_validate_json(json.dumps(missing_note))


def test_unknown_publication_state_does_not_become_an_approved_note(brief_env):
    ready = brief_env
    metadata(ready, publication=Publication(status="corrupt"))
    assert execute(ready).error.code == "BRIEF_RULES_REJECTED"


def test_pending_only_publishes_links_but_no_delivered_facts(brief_env):
    ready = brief_env
    ready.draft = pending_draft(ready)
    result = execute(ready)
    assert result.ok and result.data["status"] == "completed"
    document = ready.store.get(result.data["brief_id"], "user-1")["document"]
    assert not document.items and len(document.unverified_items) == 1
    assert document.unverified_items[0].url == ready.meta.url
    assert counts(ready) == (1, 0, 1)
    assert "待核实" in document.unverified_items[0].reason


def test_pending_items_do_not_reduce_or_exceed_main_news_limit(brief_env):
    from tests.test_briefs import subscription

    ready = brief_env
    subscription(ready, max_items=1)
    candidates = ready.store.rules.sources.candidates.known_urls(ready.env.run.id, "user-1")
    other = next(c for c in candidates.values() if c.candidate_id != ready.meta.candidate_id)
    ready.draft["unverified_items"] = [
        {"candidate_id": other.candidate_id, "reason": "补充来源尚未取得正文，真实性待核实"}
    ]
    result = execute(ready)
    assert result.ok
    document = ready.store.get(result.data["brief_id"], "user-1")["document"]
    assert len(document.items) == 1 and len(document.unverified_items) == 1
    assert document.unverified_items[0].url == other.material.url
    assert counts(ready) == (1, 1, 1)


@pytest.mark.parametrize("change", ["foreign", "duplicate", "also_main", "forged_url", "exclusion"])
def test_pending_ownership_count_exclusion_and_server_links(brief_env, change):
    ready = brief_env
    value = pending_draft(ready)
    if change == "foreign":
        value["unverified_items"][0]["candidate_id"] = "cand-" + "0" * 32
    elif change == "duplicate":
        value["unverified_items"] *= 2
    elif change == "also_main":
        value["items"] = ready.draft["items"]
        value["uncovered_topics"] = []
    elif change == "forged_url":
        value["unverified_items"][0]["url"] = "https://attacker.example.com"
    else:
        from tests.test_briefs import subscription

        subscription(ready, exclude_keywords=("BLOCKED",))
        value["unverified_items"][0]["reason"] = "BLOCKED 内容待核实"
    ready.draft = value
    result = execute(ready)
    assert result.error.code == (
        "INVALID_ARGUMENTS" if change == "forged_url" else "BRIEF_RULES_REJECTED"
    )
    assert counts(ready) == (0, 0, 0)


def test_pending_links_are_frozen_in_submission_intent(brief_env):
    ready = brief_env
    ready.draft = pending_draft(ready)

    def interrupt(connection, call):
        raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError):
        execute(ready, on_finished=interrupt)
    with ready.env.database.transaction() as connection:
        row = connection.execute(
            "SELECT material_json FROM candidates WHERE id=?", (ready.meta.candidate_id,)
        ).fetchone()
        value = json.loads(row[0])
        value["url"] = "https://changed.example.com/other"
        connection.execute(
            "UPDATE candidates SET material_json=? WHERE id=?",
            (json.dumps(value), ready.meta.candidate_id),
        )
    with pytest.raises(ToolFailure, match="BRIEF_STATE_PROBLEM"):
        recover_result(ready)


def test_warning_and_pending_sections_are_present_in_fixed_email(brief_env):
    ready = brief_env
    metadata(ready, publication=Publication(status="missing"))
    scheduled(ready)
    result = execute(ready)
    record = ready.store.outbox.get(result.data["brief_id"], "user-1")
    assert "信息说明：发布时间未确认" in record.body_text
    assert ready.meta.url in record.body_text


def test_pending_only_email_distinguishes_unconfirmed_from_no_candidates(brief_env):
    ready = brief_env
    ready.draft = pending_draft(ready)
    scheduled(ready)
    result = execute(ready)
    record = ready.store.outbox.get(result.data["brief_id"], "user-1")
    assert "待核实信息（尚未确认，不计入主新闻条数）" in record.body_text
    assert ready.meta.url in record.body_text and "待核实原因" in record.body_text
    assert "暂无符合订阅条件的新闻" not in record.body_text
    assert counts(ready) == (1, 0, 1)


def test_review_receives_display_note_without_inventing_publication_date(tool_env):
    async def scenario():
        env = tool_env
        async with review_world(env, ReviewModel()) as world:
            selected = await draft_item(world)
            source_id = selected["citations"][0]["source_id"]
            citation = selected["citations"][0]
            start = citation["start_offset"] + citation["quote"].index("23%")
            citation.update(start_offset=start, end_offset=start + 3, quote="23%")
            meta, _ = world.sources.get_source(env.run.id, env.run.user_id, source_id)
            changed = meta.model_copy(update={"publication": Publication(status="missing")})
            with env.database.transaction() as connection:
                connection.execute(
                    "UPDATE sources SET metadata_json=? WHERE id=?",
                    (changed.model_dump_json(), source_id),
                )
            context, proposed, verified = checked(world, draft(selected))
            request = world.reviewer.input.build(context, proposed, verified)
            packet = json.loads(request.messages[1].content)
            assert packet["evidence"][0]["published_at"] is None
            assert packet["evidence"][0]["quote"] == "23%"
            assert "Apache 2.0" in packet["evidence"][0]["context"]
            assert "8 GB" in packet["evidence"][0]["context"]
            assert "发布时间未确认" in packet["display_notes"][0]["notes"][0]
            assert "不能仅因时间缺项拒绝" in request.messages[0].content
            ModelRequest.model_validate(request)

    asyncio.run(scenario())


def test_old_brief_json_remains_readable_without_new_display_fields(brief_env):
    ready = brief_env
    result = execute(ready)
    document = ready.store.get(result.data["brief_id"], "user-1")["document"].model_dump(
        mode="json"
    )
    old = deepcopy(document)
    old.pop("unverified_items")
    for item in old["items"]:
        item.pop("verification_notes")
        for citation in item["citations"]:
            citation.pop("publication_status")
    restored = BriefDocument.model_validate_json(json.dumps(old))
    assert not restored.unverified_items and not restored.items[0].verification_notes


def test_agent_moves_disputed_news_to_linked_pending_after_review_feedback(
    tool_env, response_factory
):
    async def scenario():
        env = tool_env
        reviews = ReviewModel(
            {
                "approved": False,
                "issues": [{"field": "draft.items[0].summary", "issue": "关键事实仍需补查确认"}],
            },
            {"approved": True, "issues": []},
        )
        async with review_world(env, reviews) as world:
            item = await draft_item(world)
            meta, _ = world.sources.get_source(
                env.run.id, env.run.user_id, item["citations"][0]["source_id"]
            )

            def move_to_pending(request):
                result = json.loads(request.messages[-1].content)
                assert result["error"]["code"] == "BRIEF_REVIEW_REJECTED"
                assert meta.candidate_id in result["error"]["message"]
                assert "unverified_items" in result["error"]["message"]
                return response_factory(
                    (
                        "pending",
                        "submit_brief",
                        {
                            "draft": {
                                "title": "每日简报与待核实信息",
                                "items": [],
                                "uncovered_topics": [
                                    {
                                        "topic": "开源模型",
                                        "reason": "关键事实仍需补查，暂未确认主新闻",
                                    }
                                ],
                                "shortfall_reason": "候选仍待核实，附原文供核查",
                                "unverified_items": [
                                    {"candidate_id": meta.candidate_id, "reason": "关键事实待核实"}
                                ],
                            }
                        },
                    )
                )

            conversations = ConversationStore(env.calls)
            runner = AgentRunner(
                ScriptedModel(
                    [
                        response_factory(("main", "submit_brief", {"draft": draft(item)})),
                        move_to_pending,
                    ]
                ),
                world.executor,
                conversations,
                env.settings,
            )
            done = await runner.run(env.run.id, env.run.user_id)
            assert done.status == "completed" and len(reviews.requests) == 2
            with env.database.connection() as connection:
                row = connection.execute(
                    "SELECT id FROM briefs WHERE run_id=?", (done.id,)
                ).fetchone()
                assert (
                    connection.execute("SELECT count(*) FROM delivered_history").fetchone()[0] == 0
                )
            document = world.briefs.get(row[0], env.run.user_id)["document"]
            assert not document.items and document.unverified_items[0].url == meta.url

    asyncio.run(scenario())
