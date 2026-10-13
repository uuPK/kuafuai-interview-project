"""Fictional fixed evidence shared by offline contracts and opt-in model review."""

import json
from contextlib import asynccontextmanager

from app.agent.brief_rules import BriefRules
from app.agent.briefs import BriefDraft
from app.agent.review import IndependentReviewer
from app.storage.briefs import BriefStore
from app.tools.context import bind_context
from app.tools.executor import ToolExecutor
from app.tools.models import ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.submit import register_submission
from tests.research_support import ARTICLES, ResearchWorld

MODEL_SUMMARY = (
    "Atlas v2 权重已按 Apache 2.0 开放，原文报告内存为8 GB；"
    "在 H100、batch size 1 条件下，相比 Atlas v1 吞吐量提高23%。"
)
MODEL_FACTS = (
    "Atlas v2 权重采用 Apache 2.0 许可证",
    "原文报告内存为8 GB",
    "H100、batch size 1 条件下，相比 Atlas v1 吞吐量提高23%",
)


@asynccontextmanager
async def review_world(env, model, *, injection=False, zero_first=False):
    async with ResearchWorld(env, injection=injection, zero_first=zero_first) as world:
        registry = ToolRegistry()
        for schema in world.registry.schemas():
            name = schema["function"]["name"]
            if name != "submit_brief":
                registry.register(world.registry.get(name))
        world.briefs = BriefStore(world.research, BriefRules(world.sources, env.settings))
        world.reviewer = IndependentReviewer(model, world.research, world.sources, env.settings)
        register_submission(
            registry,
            world.briefs,
            reviewer=world.reviewer,
            review_basis="fixture" if getattr(model, "fixture", False) else "independent",
        )
        world.registry = registry
        world.executor = ToolExecutor(registry, env.calls, env.settings)
        yield world


async def draft_item(world, key="m2"):
    env = world.env
    if not world.discoveries:
        await world.executor.execute(
            env.run.id,
            env.run.user_id,
            ToolInvocation(
                tool_call_id="discover",
                name="web_search",
                arguments='{"query":"Atlas release","limit":10}',
            ),
        )
    with env.database.connection() as connection:
        rows = connection.execute(
            "SELECT id, material_json FROM candidates WHERE run_id=?", (env.run.id,)
        ).fetchall()
    candidate_id = next(
        row["id"] for row in rows if json.loads(row["material_json"])["url"].endswith("/" + key)
    )
    result = await world.executor.execute(
        env.run.id,
        env.run.user_id,
        ToolInvocation(
            tool_call_id="fetch-" + key,
            name="fetch_url",
            arguments=json.dumps({"candidate_id": candidate_id}),
        ),
    )
    meta, index = world.sources.get_source(env.run.id, env.run.user_id, result.data["source_id"])
    context = bind_context(env.runs, env.settings.data_dir, env.run.id, env.run.user_id)
    body = world.sources.files.verify(context, meta, index)
    quote = ARTICLES[key][2]
    start = body.index(quote)
    return {
        "title": "Atlas v2 开放权重与性能变化",
        "summary": MODEL_SUMMARY,
        "topics": ["开源模型"],
        "event_id": "atlas-v2",
        "entities": ["Atlas"],
        "product_version": "v2",
        "selection_reason": "许可证与内存信息对模型选型有价值，引用保留性能比较条件",
        "key_facts": list(MODEL_FACTS),
        "citations": [
            {
                "source_id": meta.source_id,
                "start_offset": start,
                "end_offset": start + len(quote),
                "quote": quote,
            }
        ],
    }


def draft(*items):
    return {
        "title": "每日AI新闻",
        "items": list(items),
        "uncovered_topics": [],
        "shortfall_reason": "本次已核实的固定正文支持所列条目，其余候选仍缺进一步核实材料",
    }


def checked(world, value):
    env = world.env
    context = bind_context(env.runs, env.settings.data_dir, env.run.id, env.run.user_id)
    model = BriefDraft.model_validate_json(json.dumps(value, ensure_ascii=False))
    result, rejected = world.briefs.rules.check(context, model)
    assert rejected is None
    return context, model, result
