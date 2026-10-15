"""Explicit local browser fixture. Never imported by production app/Worker entry points."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import uvicorn

from app.agent.briefs import ReviewOutcome
from app.agent.protocol import AssistantMessage, FunctionCall, ModelResponse, ModelToolCall
from app.agent.runner import AgentRunner
from app.config import Settings
from app.main import create_app
from app.providers.bocha import BochaClient
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.search import register_search
from app.tools.submit import register_submission
from app.worker.service import Worker


class FixtureModel:
    async def complete(self, request):
        task = json.loads(next(m.content for m in request.messages if m.role == "user"))
        searched = any(
            getattr(m, "tool_call_id", None) == "fixture-search" for m in request.messages
        )
        name = "submit_brief" if searched else "web_search"
        args = (
            {
                "draft": {
                    "title": "[fixture] <img src=x onerror=alert(1)> 暂无符合条件的新闻",
                    "items": [],
                    "uncovered_topics": [
                        {"topic": t, "reason": "[fixture] 固定搜索为空"} for t in task["topics"]
                    ],
                    "shortfall_reason": "[fixture] 仅验证产品流程，未请求真实新闻服务",
                }
            }
            if searched
            else {"query": "fixture empty"}
        )
        return ModelResponse(
            message=AssistantMessage(
                tool_calls=(
                    ModelToolCall(
                        id="fixture-submit" if searched else "fixture-search",
                        function=FunctionCall(
                            name=name, arguments=json.dumps(args, ensure_ascii=False)
                        ),
                    ),
                )
            ),
            finish_reason="tool_calls",
        )


def fixture_app(directory):
    config = Settings(
        _env_file=None, data_dir=directory, session_secret="browser-fixture-secret-" * 3
    )
    app = create_app(config)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            runtime = application.state
            fake = Settings(
                _env_file=None,
                data_dir=directory,
                bocha_search_url="https://search.example.com",
                bocha_api_key="browser-search-fixture-key-value",
            )
            async with BochaClient(
                fake,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200, json={"code": 200, "data": {"webPages": {"value": []}}}
                    )
                ),
            ) as bocha:
                registry = ToolRegistry()
                register_search(registry, bocha, runtime.candidate_store, config)

                async def reviewer(*args):
                    return ReviewOutcome(approved=True, issues=())

                register_submission(
                    registry, runtime.brief_store, reviewer=reviewer, review_basis="fixture"
                )
                runner = AgentRunner(
                    FixtureModel(),
                    ToolExecutor(registry, runtime.call_store, config),
                    runtime.conversation_store,
                    config,
                )

                async def maintenance():
                    await asyncio.to_thread(runtime.schedule_store.tick)

                worker = Worker(runner, maintenance=maintenance)
                stop = asyncio.Event()
                work = asyncio.create_task(worker.serve(stop))
                try:
                    yield
                finally:
                    stop.set()
                    await work

    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(fixture_app(args.data_dir), host="127.0.0.1", port=args.port)
