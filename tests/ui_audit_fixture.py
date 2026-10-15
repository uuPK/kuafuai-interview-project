"""Isolated history samples for browser audits; no real research or delivery claim."""

import asyncio
import sys
from uuid import uuid4

from app.agent.briefs import BriefDocument
from app.config import Settings
from app.runtime import open_runtime
from app.storage.models import RunRequest, StateChange, timestamp


async def seed(directory):
    config = Settings(_env_file=None, data_dir=directory)
    async with open_runtime(config) as runtime:
        with runtime.database.connection() as connection:
            owner = connection.execute(
                "SELECT id FROM auth_users WHERE email='browser-stage4@example.com'"
            ).fetchone()[0]
            previous = connection.execute(
                "SELECT id FROM runs WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (owner,)
            ).fetchone()[0]
        original = runtime.run_store.get_run(previous, owner)
        for i in range(12):
            title = f"[fixture] 历史分页样例 {i + 1:02}"
            if i == 11:
                title = "[fixture] 长文本与引用验收 " + "W" * 220
            document = BriefDocument.model_validate(
                {
                    "title": title,
                    "items": (
                        {
                            "title": "[fixture] <img src=x onerror=alert(1)> " + "X" * 220,
                            "summary": "[fixture] 中文摘要；只验证页面展示与引用。" + "S" * 700,
                            "topics": ("开源模型",),
                            "event_id": f"fixture-event-{i}",
                            "selection_reason": "[fixture] 用于检查正文、理由与引用显示",
                            "key_facts": ("[fixture] 固定测试事实",),
                            "citations": (
                                {
                                    "source_id": "src-" + uuid4().hex,
                                    "start_offset": 0,
                                    "end_offset": 7,
                                    "quote": "固定测试正文。" + "Q" * 300,
                                    "url": "https://example.com/fixture/source",
                                    "content_hash": "0" * 64,
                                    "published_at": runtime.run_store.now(),
                                },
                            ),
                        },
                    ),
                }
            )
            run = runtime.run_store.create_run(
                RunRequest(
                    user_id=owner,
                    subscription_id=original.subscription_id,
                    subscription=original.subscription,
                )
            )
            run = runtime.run_store.transition(run.id, owner, StateChange(status="running"))
            with runtime.database.transaction() as connection:
                runtime.run_store._transition(
                    connection, run, StateChange(status="completed"), runtime.run_store.now()
                )
                connection.execute(
                    "INSERT INTO briefs VALUES(?,?,?,?,?)",
                    (
                        "brief-" + uuid4().hex,
                        run.id,
                        timestamp(runtime.run_store.now()),
                        "fixture",
                        document.model_dump_json(),
                    ),
                )
                for n in range(55 if i == 11 else 1):
                    runtime.run_store._append_event(
                        connection,
                        run.id,
                        "progress_updated",
                        runtime.run_store.now(),
                        {"reason": f"[fixture] 页面事件分页 {n + 1}"},
                    )
        print("Created 12 explicit fixture briefs, including long text and event pagination.")


if __name__ == "__main__":
    asyncio.run(seed(sys.argv[1]))
