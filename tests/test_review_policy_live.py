"""Opt-in real reviewer decisions over fictional immutable evidence; no mail."""

import asyncio
import os
import time

import pytest

from app.providers.deepseek import DeepSeekClient
from tests.brief_support import review_world
from tests.test_briefs import invocation
from tests.test_review_live import environment, report
from tests.test_review_policy import two_items

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_REVIEW_LIVE") != "1", reason="requires NEWS_REVIEW_LIVE=1 and DeepSeek"
)


def test_real_reviewer_retains_good_item_beside_unsupported_number():
    async def scenario():
        env = environment("per-item-mixed")
        started = time.monotonic()
        async with DeepSeekClient(env.settings) as client, review_world(env, client) as world:
            value = await two_items(world)
            value["items"][0]["summary"] = value["items"][0]["summary"].replace("23%", "99%")
            value["items"][0]["key_facts"] = [
                fact.replace("23%", "99%") for fact in value["items"][0]["key_facts"]
            ]
            first = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "a")
            )
            result = await world.executor.execute(
                env.run.id, env.run.user_id, invocation(value, "b")
            )
            saved = (
                world.briefs.get(result.data["brief_id"], env.run.user_id) if result.ok else None
            )
            report(
                env,
                "per-item-mixed",
                first=first.model_dump(mode="json"),
                result=result.model_dump(mode="json"),
                document=saved["document"].model_dump(mode="json") if saved else None,
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
            assert not first.ok and first.error.code == "BRIEF_REVIEW_REJECTED"
            assert result.ok and result.data["review_status"] == "partial"
            assert [item.event_id for item in saved["document"].items] == ["atlas-v1"]
            assert len(saved["document"].unverified_items) == 1
            with env.database.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM delivered_history").fetchone()[0] == 1
                )
                assert connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 0

    asyncio.run(scenario())
