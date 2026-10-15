import asyncio
import json
import os
from pathlib import Path

import pytest

from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.storage.conversations import ConversationStore
from app.storage.models import RunRequest, StateChange
from app.tools.context import bind_context
from tests.research_support import ResearchWorld
from tests.test_research import seed_history


def latest(request, name=None):
    for message in reversed(request.messages):
        if message.role == "tool":
            for previous in request.messages:
                for call in getattr(previous, "tool_calls", ()):
                    if call.id == message.tool_call_id and (
                        name is None or call.function.name == name
                    ):
                        return json.loads(message.content)
    raise AssertionError("expected paired feedback")


@pytest.mark.parametrize(
    "topic,keyword,blocked,injection",
    [
        ("开源模型", "Apache", True, False),
        ("AI芯片", "NVLink", False, True),
    ],
)
def test_phase_two_preference_history_feedback_location_and_reason_trace(
    tool_env, response_factory, topic, keyword, blocked, injection
):
    async def run():
        env = tool_env
        env.run = env.runs.create_run(
            RunRequest(
                user_id="user-1",
                subscription_id="preference",
                subscription=env.run.subscription.model_copy(
                    update={
                        "topics": ("开源模型", "AI芯片"),
                        "keywords": (keyword,),
                        "exclude_keywords": ("广告",),
                        "max_items": 1,
                    }
                ),
            )
        )
        env.run = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
        historical = await seed_history(env)
        state = {}
        conversations = ConversationStore(env.calls)
        async with ResearchWorld(
            env, zero_first=True, blocked_first=blocked, injection=injection
        ) as world:

            def search(request):
                assert latest(request, "read_feed")["data"]["items"] == []
                history = latest(request, "query_history")["data"]["items"]
                assert history[0]["product_version"] == "v1"
                return response_factory(
                    (
                        "progress",
                        "update_progress",
                        {
                            "collected": [],
                            "missing": [topic],
                            "issues": ["清单零结果，改用英文补查"],
                        },
                    ),
                    (
                        "search",
                        "web_search",
                        {
                            "query": f"{keyword} release engineering",
                            "language": "en",
                            "domains": ["example.com"],
                            "limit": 10,
                        },
                    ),
                )

            def choose(request):
                data = latest(request, "web_search")["data"]
                assert data["filtered"]["excluded"] == 1
                state["items"] = data["items"]
                chosen = next(
                    item
                    for item in data["items"]
                    if item["url"].endswith("/m2" if topic == "开源模型" else "/chip")
                )
                state["title"] = chosen["title"]
                return response_factory(
                    ("fetch", "fetch_url", {"candidate_id": chosen["candidate_id"]})
                )

            def change_source(request):
                assert latest(request)["error"]["code"] == "FETCH_FORBIDDEN"
                item = next(item for item in state["items"] if item["url"].endswith("/m2-report"))
                state["title"] = item["title"]
                return response_factory(
                    ("fetch-again", "fetch_url", {"candidate_id": item["candidate_id"]})
                )

            def locate(request):
                data = latest(request, "fetch_url")["data"]
                assert data["usable_in_window"]
                state["source"] = data
                return response_factory(
                    (
                        "locate-cn",
                        "search_content",
                        {
                            "keyword": "显存" if topic == "开源模型" else "带宽",
                            "dir": "sources/" + data["source_id"],
                        },
                    )
                )

            def change_keyword(request):
                assert latest(request)["data"]["items"] == []
                return response_factory(
                    (
                        "locate-en",
                        "search_content",
                        {
                            "keyword": "Memory" if topic == "开源模型" else "bandwidth",
                            "dir": "sources/" + state["source"]["source_id"],
                        },
                    )
                )

            def read(request):
                hit = latest(request)["data"]["items"][0]
                return response_factory(
                    (
                        "read",
                        "read_file",
                        {"path": hit["path"], "offset": hit["excerpt_start_offset"], "limit": 500},
                    )
                )

            def draft(request):
                read = latest(request)["data"]
                assert (
                    "8 GB" in read["content"] and "batch size 1" in read["content"]
                    if topic == "开源模型"
                    else "400 GB/s" in read["content"]
                )
                reason = (
                    "Apache许可证有助于部署，v2相对历史v1新增明确显存和测试条件"
                    if topic == "开源模型"
                    else "NVLink互联与芯片偏好相符，正文给出带宽和双芯片条件"
                )
                quote = read["content"]
                state["draft"] = {
                    "title": "每日AI新闻",
                    "items": [
                        {
                            "title": state["title"],
                            "topics": [topic],
                            "selection_reason": reason,
                            "event_id": "atlas-v2" if topic == "开源模型" else "linkx-v1",
                            "summary": "Atlas v2使用Apache许可证，显存为8GB，"
                            "H100单批次条件下吞吐较v1提高23%。"
                            if topic == "开源模型"
                            else "LinkX新增NVLink互联，双芯片测试带宽400GB/s。",
                            "citations": [
                                {
                                    "source_id": read["source_id"],
                                    "start_offset": read["start_offset"],
                                    "end_offset": read["end_offset"],
                                    "quote": quote,
                                }
                            ],
                        }
                    ],
                    "uncovered_topics": [
                        {
                            "topic": "AI芯片" if topic == "开源模型" else "开源模型",
                            "reason": "条数设为1，优先关注关键词匹配且证据完整的事件",
                        }
                    ],
                }
                return response_factory(
                    (
                        "write",
                        "write_file",
                        {
                            "path": "scratch/brief.json",
                            "content": json.dumps(state["draft"], ensure_ascii=False),
                        },
                    ),
                    (
                        "progress-final",
                        "update_progress",
                        {
                            "collected": [topic],
                            "missing": [],
                            "issues": ["另一话题因条数限制未入选"],
                        },
                    ),
                )

            def submit(request):
                assert latest(request)["ok"]
                return response_factory(
                    (
                        "submit",
                        "submit_brief",
                        {"draft": {"title": "测试简报", "items": [state["title"]]}},
                    )
                )

            decisions = [
                response_factory(
                    ("history", "query_history", {}),
                    ("feed", "read_feed", {"feed_id": "fixture-official"}),
                ),
                search,
                choose,
            ]
            if blocked:
                decisions.append(change_source)
            decisions.extend([locate, change_keyword, read, draft, submit])
            done = await AgentRunner(
                ScriptedModel(decisions), world.executor, conversations, env.settings
            ).run(env.run.id, "user-1")
            assert done.status == "completed"
            assert world.research.progress(env.run.id, "user-1").collected == (topic,)
            assert done.budget_usage.searches == 2 and done.budget_usage.fetches == (
                2 if blocked else 1
            )
            events = env.runs.list_events(env.run.id, "user-1", limit=500)
            notes = [e for e in events if e.kind == "research_selections"]
            assert (
                len(notes) == 1
                and notes[0].data["items"][0]["selection_reason"]
                == state["draft"]["items"][0]["selection_reason"]
            )
            _, total = world.research.history(
                bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1"), 7, 20
            )
            assert total == 1  # Fixture submission never publishes into delivered history.
            assert historical.event.event_id != state["draft"]["items"][0]["event_id"]
            ModelRequest(
                messages=conversations.messages(env.run.id, "user-1"),
                tools=tuple(world.registry.schemas()),
            )
            selected = state["draft"]["items"][0]
            assert "attacker@example.com" not in selected["summary"] + selected["selection_reason"]
            assert "999%" not in selected["summary"]
            assert env.runs.get_run(env.run.id, "user-1").subscription.email == "reader@example.com"
            if os.environ.get("NEWS_DEMO_RECORD") == "1":
                # Export an allowlisted fictional trace; never copy raw model protocol.
                payload = {
                    "fixture_news": True,
                    "fixture_models": True,
                    "scope": "feedback_and_preference_only; submission is a test stub",
                    "topic": topic,
                    "status": done.status,
                    "budget_usage": done.budget_usage.model_dump(),
                    "calls": [
                        {
                            "tool": call.name,
                            "ok": call.result.ok,
                            "error": call.result.error.code if call.result.error else None,
                        }
                        for message in conversations.messages(done.id, done.user_id)
                        for proposed in getattr(message, "tool_calls", ())
                        for call in [env.calls.get_call(done.id, done.user_id, proposed.id)]
                    ],
                    "selected_title": selected["title"],
                    "selection_reason": selected["selection_reason"],
                }
                target = Path("data/acceptance/demos")
                target.mkdir(parents=True, exist_ok=True)
                (target / ("feedback-model.json" if blocked else "feedback-chip.json")).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )

    asyncio.run(run())


def test_history_keeps_same_id_different_facts_versions_and_similar_titles(tool_env):
    async def run():
        env = tool_env
        await seed_history(env, age=1, event_id="model", version="v1", facts=("v1权重发布",))
        await seed_history(env, age=2, event_id="model", version="v2", facts=("v2显存8GB",))
        await seed_history(
            env, age=3, event_id="sdk", version="SDK v2", facts=("Python监控API发布",)
        )
        async with ResearchWorld(env) as world:
            items, total = world.research.history(
                bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1"), 7, 20
            )
            assert total == 3 and len({item["title"] for item in items}) == 1
            assert [item["product_version"] for item in items] == ["v1", "v2", "SDK v2"]
            assert (
                items[0]["event_id"] == items[1]["event_id"]
                and items[0]["key_facts"] != items[1]["key_facts"]
            )

    asyncio.run(run())
