"""Opt-in real DeepSeek quality judgments on explicitly fictional fixed evidence."""

import asyncio
import json
import os
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent.prompts import initial_messages
from app.agent.protocol import (
    AssistantMessage,
    FunctionCall,
    InputMessage,
    ModelResponse,
    ModelToolCall,
)
from app.agent.runner import AgentRunner
from app.config import Settings
from app.harness.redaction import Redactor
from app.providers.deepseek import DeepSeekClient
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.models import RunRequest, StateChange, SubscriptionSnapshot
from app.storage.runs import RunStore
from tests.brief_support import MODEL_FACTS, draft, draft_item, review_world
from tests.test_briefs import invocation
from tests.test_research import seed_history

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_REVIEW_LIVE") != "1",
    reason="requires NEWS_REVIEW_LIVE=1 and configured DeepSeek",
)


def environment(profile):
    root = Path("data/acceptance/review-live-2026-10-13") / profile / uuid4().hex
    config = Settings(
        data_dir=root,
        deepseek_thinking_enabled=False,
        external_max_attempts=2,
        max_model_requests=12,
    )
    config.require_service_config("deepseek")
    database = Database(config.database_path)
    database.initialize()
    runs = RunStore(database, config)
    run = runs.create_run(
        RunRequest(
            user_id="review-fixture",
            subscription_id="review-fixture",
            subscription=SubscriptionSnapshot(
                nickname="核对样例",
                email="fixture@example.com",
                topics=("开源模型",),
                keywords=("Apache",),
                max_items=2,
            ),
        )
    )
    run = runs.transition(run.id, run.user_id, StateChange(status="running"))
    return SimpleNamespace(
        settings=config, database=database, runs=runs, run=run, calls=CallStore(runs)
    )


def report(env, name, **values):
    payload = Redactor(env.settings).clean(
        {
            "scenario": name,
            "fixture_news": True,
            "model": env.settings.deepseek_model,
            "run_id": env.run.id,
            **values,
        }
    )
    (env.settings.data_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "scenario,approved",
    [
        ("valid_conditions", True),
        ("unsupported_number", False),
        ("omitted_conditions", False),
        ("duplicate_event", False),
        ("unchanged_history", False),
        ("new_version", True),
        ("separate_sdk_event", True),
        ("source_instructions", True),
    ],
)
def test_live_independent_quality_review(scenario, approved):
    async def run():
        env = environment(scenario)
        before = time.monotonic()
        result = None
        async with (
            DeepSeekClient(env.settings) as client,
            review_world(env, client, injection=scenario == "source_instructions") as world,
        ):
            item = await draft_item(world)
            if scenario == "unsupported_number":
                item["summary"] = item["summary"].replace("23%", "99%")
                item["key_facts"][-1] = item["key_facts"][-1].replace("23%", "99%")
            if scenario == "omitted_conditions":
                item["summary"] = (
                    "Atlas v2 权重已按 Apache 2.0 开放，内存为8 GB；相比 Atlas v1 吞吐量提高23%。"
                )
                item["key_facts"][-1] = "相比 Atlas v1 吞吐量提高23%"
            if scenario in {"unchanged_history", "new_version"}:
                await seed_history(
                    env,
                    user=env.run.user_id,
                    facts=MODEL_FACTS
                    if scenario == "unchanged_history"
                    else ("Atlas v1 权重已开放下载",),
                    event_id="atlas-v2" if scenario == "unchanged_history" else "atlas-v1",
                    version="v2" if scenario == "unchanged_history" else "v1",
                )
            items = [item]
            if scenario == "duplicate_event":
                duplicate = await draft_item(world, "m2-report")
                duplicate["event_id"] = "different-model-proposed-id"
                duplicate["title"] = "社区报道新模型上线"
                items.append(duplicate)
            if scenario == "separate_sdk_event":
                sdk = await draft_item(world, "sdk")
                sdk.update(
                    title="Atlas SDK v2 增加遥测接口",
                    event_id="atlas-sdk-v2",
                    summary="Atlas SDK v2 新增 Python 遥测 API，这是独立于模型权重的SDK发布。",
                    key_facts=["Atlas SDK v2 新增 Python 遥测 API"],
                    selection_reason="开发工具接口更新对模型工程实践有价值，与权重发布是独立事件",
                )
                items.append(sdk)
            value = draft(*items)
            try:
                result = await world.executor.execute(
                    env.run.id, env.run.user_id, invocation(value)
                )
                assert result.ok is approved
                if not approved:
                    assert result.error.code == "BRIEF_REVIEW_REJECTED" and result.error.details
                else:
                    saved = world.briefs.get(result.data["brief_id"], env.run.user_id)
                    assert saved["review_basis"] == "independent"
            finally:
                with env.database.connection() as connection:
                    rows = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM brief_reviews WHERE run_id=?", (env.run.id,)
                        )
                    ]
                report(
                    env,
                    scenario,
                    expected_approved=approved,
                    draft=value,
                    result=result.model_dump(mode="json") if result else None,
                    review_records=rows,
                    budget_usage=env.runs.get_run(
                        env.run.id, env.run.user_id
                    ).budget_usage.model_dump(),
                    elapsed_seconds=round(time.monotonic() - before, 3),
                    full_real_news_quality_acceptance=False,
                )

    asyncio.run(run())


@pytest.mark.parametrize("correction", ["number", "range"])
def test_live_research_model_corrects_review_feedback_then_submits(correction):
    async def run():
        env = environment("revision-" + correction)
        conversations = ConversationStore(env.calls)
        before = time.monotonic()
        done = None
        async with DeepSeekClient(env.settings) as client, review_world(env, client) as world:
            value = draft(await draft_item(world))
            invalid = deepcopy(value)
            if correction == "number":
                invalid["items"][0]["summary"] = invalid["items"][0]["summary"].replace(
                    "23%", "99%"
                )
                invalid["items"][0]["key_facts"][-1] = invalid["items"][0]["key_facts"][-1].replace(
                    "23%", "99%"
                )
            else:
                invalid["items"][0]["citations"][0]["start_offset"] += 1
                invalid["items"][0]["citations"][0]["end_offset"] += 1
            system, user = initial_messages(env.run)
            task = json.loads(user.content)
            task["fixture_acceptance"] = (
                "这是标记fixture的固定新闻材料核对验收，已发现并抓取正文。"
                "第一份待核对草稿含人为加入的问题；根据提交工具的具体反馈自主选择重读原文、改稿或替换。"
                "保留完整测试条件、比较对象，修正summary及key_facts，引用必须逐字符一致。"
                "当前正文路径为sources/"
                + value["items"][0]["citations"][0]["source_id"]
                + "/body.txt。"
            )
            conversations.initialize(
                env.run.id,
                env.run.user_id,
                (system, InputMessage(role="user", content=json.dumps(task, ensure_ascii=False))),
            )
            seeded = ModelResponse(
                message=AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            id="seed-invalid",
                            function=FunctionCall(
                                name="submit_brief", arguments=invocation(invalid).arguments
                            ),
                        ),
                    )
                ),
                finish_reason="tool_calls",
            )
            conversations.save_response(env.run.id, env.run.user_id, seeded)
            try:
                done = await AgentRunner(client, world.executor, conversations, env.settings).run(
                    env.run.id, env.run.user_id
                )
                assert done.status == "completed"
                first = env.calls.get_call(env.run.id, env.run.user_id, "seed-invalid")
                assert first.result.error.code == (
                    "BRIEF_REVIEW_REJECTED" if correction == "number" else "BRIEF_RULES_REJECTED"
                )
                with env.database.connection() as connection:
                    brief_id = connection.execute(
                        "SELECT id FROM briefs WHERE run_id=?", (env.run.id,)
                    ).fetchone()[0]
                final = world.briefs.get(brief_id, env.run.user_id)["document"]
                text = final.model_dump_json()
                assert "99%" not in text and "23%" in text and "H100" in text
            finally:
                with env.database.connection() as connection:
                    calls = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM tool_calls WHERE run_id=? "
                            "ORDER BY created_at, tool_call_id",
                            (env.run.id,),
                        )
                    ]
                    reviews = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM brief_reviews WHERE run_id=?", (env.run.id,)
                        )
                    ]
                    saved_briefs = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM briefs WHERE run_id=?", (env.run.id,)
                        )
                    ]
                report(
                    env,
                    "revision-" + correction,
                    status=done.status if done else None,
                    calls=calls,
                    reviews=reviews,
                    briefs=saved_briefs,
                    research_responses=[
                        r.model_dump(mode="json")
                        for r in conversations.responses(env.run.id, env.run.user_id)
                    ],
                    seeded_first_draft=True,
                    elapsed_seconds=round(time.monotonic() - before, 3),
                    budget_usage=env.runs.get_run(
                        env.run.id, env.run.user_id
                    ).budget_usage.model_dump(),
                    full_real_news_quality_acceptance=False,
                )

    asyncio.run(run())
