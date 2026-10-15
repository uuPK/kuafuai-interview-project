"""Create one isolated UI sample by losing an actual loopback SMTP acknowledgement."""

import asyncio
import sys
from uuid import uuid4

from app.agent.briefs import BriefDocument
from app.providers.smtp import SMTPTransport
from app.runtime import open_runtime
from app.storage.models import RunRequest, StateChange, timestamp
from tests.smtp_support import smtp_server, smtp_settings


async def seed(directory, port):
    config = smtp_settings(directory, port).model_copy(
        update={
            "smtp_test_recipient": "browser-stage4@example.com",
        }
    )
    async with open_runtime(config) as runtime:
        with runtime.database.connection() as connection:
            user = connection.execute(
                "SELECT id FROM auth_users WHERE email='browser-stage4@example.com'"
            ).fetchone()[0]
            previous = connection.execute(
                "SELECT id FROM runs WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (user,)
            ).fetchone()[0]
        original = runtime.run_store.get_run(previous, user)
        run = runtime.run_store.create_run(
            RunRequest(
                user_id=user,
                subscription_id=original.subscription_id,
                subscription=original.subscription,
                mode="scheduled",
                scheduled_for=runtime.run_store.now(),
            )
        )
        run = runtime.run_store.transition(run.id, user, StateChange(status="running"))
        brief_id = "brief-" + uuid4().hex
        document = BriefDocument(
            title="[fixture] 邮件恢复与显式重发验收",
            items=(),
            shortfall_reason="[fixture] 本地产品链路样例，不代表真实新闻质量",
        )
        with runtime.database.transaction() as connection:
            runtime.run_store._transition(
                connection, run, StateChange(status="no_news"), runtime.run_store.now()
            )
            connection.execute(
                "INSERT INTO briefs VALUES(?,?,?,?,?)",
                (
                    brief_id,
                    run.id,
                    timestamp(runtime.run_store.now()),
                    "fixture",
                    document.model_dump_json(),
                ),
            )
            runtime.outbox_store.reserve(connection, run.id, user, brief_id)
        record, message_id, token = runtime.dispatch_store.claim()
        assert record.brief_id == brief_id
        result = SMTPTransport(config).send(
            record, message_id, admit=lambda: runtime.dispatch_store.admit(brief_id, token)
        )
        assert runtime.dispatch_store.finish(brief_id, token, result) == "unknown"
        print(brief_id)


if __name__ == "__main__":
    with smtp_server("drop_after") as server:
        asyncio.run(seed(sys.argv[1], server.server_address[1]))
