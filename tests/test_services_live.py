"""Explicit real-service acceptance. Unique ignored data; one test email per mail invocation."""

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.runner import AgentRunner
from app.config import Settings
from app.harness.redaction import Redactor
from app.providers.feeds import FeedClient
from app.providers.smtp import SMTPTransport
from app.runtime import open_runtime
from app.storage.models import RunRequest, SubscriptionSnapshot
from app.storage.outbox import MailRecord, message_hash
from app.tools.discovery import SearchParameters
from app.tools.models import ToolFailure
from app.worker.recovery import Recovery
from app.worker.service import Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_SERVICES_LIVE") != "1",
    reason="requires NEWS_SERVICES_LIVE=1; uses paid services and the configured test mailbox",
)


def configured(component):
    root = (
        Path("data/acceptance")
        / f"services-live-{datetime.now(UTC):%Y-%m-%d}"
        / component
        / uuid4().hex
    )
    root.mkdir(parents=True)
    return Settings(
        data_dir=root,
        rss_sources_file=Path("config/rss-sources.json"),
        request_timeout_seconds=45,
        external_max_attempts=2,
        deepseek_thinking_enabled=False,
        deepseek_max_tokens=3072,
        max_model_requests=20,
        max_tool_calls=50,
        run_timeout_seconds=900,
        smtp_enabled=component == "mail",
    )


def save(config, report):
    (config.data_dir / "report.json").write_text(
        json.dumps(Redactor(config).clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.mark.parametrize("provider", ["bocha", "rss"])
def test_real_discovery_provider(provider):
    async def scenario():
        config = configured("providers-" + provider)
        report = {
            "scope": "real discovery provider; no model or email requested",
            "provider": provider,
            "status": "failed",
        }
        started = time.monotonic()
        try:
            async with open_runtime(config) as runtime:
                if provider == "bocha":
                    result = await runtime.bocha_client.search(
                        SearchParameters(query="AI 模型 发布", limit=3)
                    )
                    report["bocha"] = {
                        "candidates": len(result.items),
                        "invalid": result.invalid_count,
                    }
                else:
                    feed = runtime.feed_catalog.get("nvidia-official")
                    result = await FeedClient(runtime.public_http_client, config).read(feed)
                    report["rss"] = {
                        "entries": result.received_count,
                        "candidates": len(result.items),
                    }
                assert result.items
                report["status"] = "passed"
        except ToolFailure as error:
            report["code"] = error.result.error.code
            raise
        finally:
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            save(config, report)

    asyncio.run(scenario())


@pytest.mark.parametrize("topic", ["开源模型", "AI芯片", "AI应用", "AI工程", "机器人"])
def test_real_research_and_source_citations(topic):
    async def scenario():
        config = configured("research")
        report = {
            "scope": "real model, search and public sources; preview only, no email",
            "topic": topic,
        }
        started = time.monotonic()
        async with open_runtime(config) as runtime:
            run = runtime.run_store.create_run(
                RunRequest(
                    user_id="acceptance-reader",
                    subscription_id="acceptance-preview",
                    subscription=SubscriptionSnapshot(
                        nickname="真实研究验收",
                        email="fixture@example.com",
                        topics=(topic,),
                        max_items=1,
                    ),
                )
            )
            runner = AgentRunner(
                runtime.model_client, runtime.tool_executor, runtime.conversation_store, config
            )
            worker = Worker(
                runner,
                recovery=Recovery(
                    runner, sources=runtime.source_store, materials=runtime.material_store
                ),
            )
            try:
                async with asyncio.timeout(930):
                    while True:
                        await worker.run_once()
                        done = runtime.run_store.get_run(run.id, run.user_id)
                        if done.status in {"completed", "no_news", "failed"}:
                            break
                        await asyncio.sleep(0.5)
            finally:
                worker.leases.stop_worker(worker.worker_id)
                done = runtime.run_store.get_run(run.id, run.user_id)
                report.update(
                    run_id=done.id,
                    status=done.status,
                    stop_reason=done.stop_reason,
                    elapsed_seconds=round(time.monotonic() - started, 2),
                    budget_usage=done.budget_usage.model_dump(),
                    window_start=done.window_start.isoformat(),
                    window_end=done.window_end.isoformat(),
                )
                responses = runtime.conversation_store.responses(run.id, run.user_id)
                report["research_response_tokens"] = sum(
                    r.usage.get("total_tokens", 0) for r in responses
                )
                with runtime.database.connection() as connection:
                    brief = connection.execute(
                        "SELECT id FROM briefs WHERE run_id=?", (run.id,)
                    ).fetchone()
                    calls = connection.execute(
                        "SELECT name,result_json FROM tool_calls WHERE run_id=? ORDER BY rowid",
                        (run.id,),
                    ).fetchall()
                    reviews = connection.execute(
                        "SELECT response_json FROM brief_reviews WHERE run_id=? "
                        "AND response_json IS NOT NULL",
                        (run.id,),
                    ).fetchall()
                report["review_response_tokens"] = sum(
                    json.loads(row[0]).get("usage", {}).get("total_tokens", 0) for row in reviews
                )
                report["calls"] = [
                    {
                        "tool": row[0],
                        "ok": value["ok"],
                        "error": value["error"]["code"] if value["error"] else None,
                    }
                    for row in calls
                    if row[1] and (value := json.loads(row[1]))
                ]
                report["read_calls"] = sum(row[0] == "read_file" for row in calls)
                report["search_content_calls"] = sum(row[0] == "search_content" for row in calls)
                if brief:
                    report["brief"] = runtime.catalog_store.brief(brief[0], run.user_id)
                    report["human_review"] = "pending: review against saved full source snapshots"
                save(config, report)
            assert done.status == "completed", f"{done.status}: {done.stop_reason}; report saved"
            assert len(report["brief"]["document"]["items"]) == 1
            assert report["brief"]["review_basis"] == "independent"

    asyncio.run(scenario())


def test_real_qq_smtp_to_explicit_test_recipient():
    config = configured("mail")
    config.require_service_config("smtp")
    assert config.smtp_enabled and config.smtp_test_recipient, "explicit test recipient is required"
    now, identifier = datetime.now(UTC), uuid4().hex
    subject = "每日 AI 新闻助手｜SMTP 联通测试"
    body = (
        "这是一封每日 AI 新闻助手的联通测试邮件。\n"
        "此邮件用于确认 QQ SMTP 到指定测试邮箱的交付，不包含新闻简报。\n验收标识：" + identifier
    )
    recipient = config.smtp_test_recipient
    record = MailRecord(
        brief_id="test-" + identifier,
        run_id="test-" + identifier,
        recipient=recipient,
        subject=subject,
        body_text=body,
        payload_hash=message_hash(recipient, subject, body),
        status="pending",
        attempts=0,
        created_at=now,
        updated_at=now,
    )
    # Persist intent before sending. Never automatically retry an unknown result.
    report = {
        "scope": "real external SMTP; explicit test payload, no research brief",
        "test_id": identifier,
        "status": "sending",
        "inbox_confirmation": "pending",
        "recipient_hash": hashlib.sha256(recipient.casefold().encode()).hexdigest(),
    }
    save(config, report)
    started = time.monotonic()
    outcome = SMTPTransport(config).send(record, f"<acceptance-{identifier}@daily-ai.local>")
    report.update(
        status=outcome.status,
        code=outcome.code,
        elapsed_seconds=round(time.monotonic() - started, 2),
    )
    save(config, report)
    assert outcome.status == "sent", (
        f"SMTP status={outcome.status}, code={outcome.code}; no automatic resend"
    )
