"""Stage 4 product loop: HTTP auth/preferences -> scheduler -> Agent -> real TCP SMTP."""

import asyncio
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

import httpx

from app.agent.briefs import ReviewOutcome
from app.agent.runner import AgentRunner
from app.providers.bocha import BochaClient
from app.runtime import open_runtime
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.search import register_search
from app.tools.submit import register_submission
from app.worker.service import Worker
from tests.smtp_support import smtp_server, smtp_settings
from tests.test_auth import client_for, login, register
from tests.test_product_api import preview, subscribe
from tests.ui_fixture import FixtureModel


def test_product_scheduled_mail_preview_pause_and_restart(tmp_path):
    with smtp_server() as server:
        config = smtp_settings(tmp_path, server.server_address[1])
        with client_for(config) as client:
            assert register(client).status_code == 201
            at = [datetime(2026, 10, 15, 0, tzinfo=UTC)]
            client.app.state.run_store.clock = lambda: at[0]
            saved = subscribe(client).json()["subscription"]
            assert saved["push_time"] == "09:00"

            async def execute_stage():
                async with open_runtime(config) as runtime:
                    runtime.run_store.clock = lambda: at[0]
                    search_config = config.model_copy(
                        update={
                            "bocha_search_url": "https://search.example.com",
                            "bocha_api_key": __import__("pydantic").SecretStr(
                                "fixture-search-key-value"
                            ),
                        }
                    )
                    async with BochaClient(
                        search_config,
                        transport=httpx.MockTransport(
                            lambda _: httpx.Response(
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

                        async def maintenance():
                            await asyncio.to_thread(runtime.schedule_store.tick)
                            await runtime.mailer.run_once()

                        runner = AgentRunner(
                            FixtureModel(),
                            ToolExecutor(registry, runtime.call_store, config),
                            runtime.conversation_store,
                            config,
                        )
                        worker = Worker(runner, maintenance=maintenance)
                        try:
                            assert await worker.run_once() is None
                            at[0] += timedelta(hours=1)
                            scheduled = await worker.run_once()
                            assert scheduled.mode == "scheduled" and scheduled.status == "no_news"
                            assert scheduled.window_end == at[0]
                            assert await worker.run_once() is None
                            brief = client.get("/briefs").json()["items"][0]["brief_id"]
                            mail = client.get(f"/briefs/{brief}/mail").json()["mail"]
                            assert mail["status"] == "sent" and mail["attempts"] == 1
                            assert len(server.messages) == 1
                            parsed = BytesParser(policy=policy.default).parsebytes(
                                server.messages[0]
                            )
                            assert parsed["To"] == "reader@example.com"
                            assert "暂无符合订阅条件的新闻" in parsed.get_content()
                            run_id = preview(client).json()["run_id"]
                            result = await worker.run_once()
                            assert result.id == run_id and result.mode == "preview"
                            assert await worker.run_once() is None
                            assert len(server.messages) == 1
                            subscribe(client, push_time="10:00")
                            at[0] += timedelta(hours=1)
                            assert await worker.run_once() is None  # business-day unique
                            subscribe(client, enabled=False)
                            at[0] += timedelta(days=1)
                            assert await worker.run_once() is None
                            assert len(client.get("/runs").json()["items"]) == 2
                            assert len(client.get("/briefs").json()["items"]) == 2
                        finally:
                            worker.leases.stop_worker(worker.worker_id)

            asyncio.run(execute_stage())
        with client_for(config) as reopened:
            assert login(reopened).status_code == 200
            assert len(reopened.get("/briefs").json()["items"]) == 2
            assert reopened.get("/subscription").json()["subscription"]["enabled"] is False
