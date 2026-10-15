"""Fictional stage-three fixtures and opt-in, inspectable local acceptance traces."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.agent.prompts import initial_messages
from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.config import Settings
from app.harness.redaction import Redactor
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.models import RunRequest, StateChange, SubscriptionSnapshot
from app.storage.runs import RunStore
from app.storage.snapshots import SourceMetadata, digest
from app.storage.timeline import TimelineStore
from app.worker.recovery import Recovery
from app.worker.service import Worker
from tests.test_harness import Clock


class Interrupted(BaseException):
    pass


def environment(tmp_path, scenario, **overrides):
    root = (
        Path("data/acceptance/phase-three-step-3.5") / scenario / uuid4().hex
        if os.environ.get("NEWS_PHASE_THREE_RECORD") == "1"
        else tmp_path
    )
    config = Settings(
        _env_file=None, data_dir=root, deepseek_api_key="fixture-stage-three", **overrides
    )
    clock = Clock()
    database = Database(config.database_path)
    database.initialize()
    runs = RunStore(database, config, clock=clock.now)
    run = runs.create_run(
        RunRequest(
            user_id="phase-three",
            subscription_id="fixture",
            mode="scheduled",
            scheduled_for=clock.now(),
            subscription=SubscriptionSnapshot(
                nickname="固定验收",
                email="fixture@example.com",
                topics=("开源模型",),
                max_items=1,
            ),
        )
    )
    run = runs.transition(run.id, run.user_id, StateChange(status="running"))
    return SimpleNamespace(
        settings=config, database=database, runs=runs, run=run, clock=clock, calls=CallStore(runs)
    )


def reopen(env):
    database = Database(env.database.path)
    database.initialize()
    runs = RunStore(database, env.settings, clock=env.clock.now)
    return SimpleNamespace(
        settings=env.settings,
        database=database,
        runs=runs,
        calls=CallStore(runs),
        clock=env.clock,
        run=runs.get_run(env.run.id, env.run.user_id),
    )


def worker(env, world, model):
    conversations = getattr(env, "conversations", None) or ConversationStore(env.calls)
    env.conversations = conversations
    runner = AgentRunner(model, world.executor, conversations, env.settings)
    return Worker(
        runner, recovery=Recovery(runner, sources=world.sources, materials=world.materials)
    )


def seed(env, response):
    env.conversations = ConversationStore(env.calls)
    env.conversations.initialize(env.run.id, env.run.user_id, initial_messages(env.run))
    env.conversations.save_response(env.run.id, env.run.user_id, response)


def state(env):
    run = env.runs.get_run(env.run.id, env.run.user_id)
    sources = []
    with env.database.connection() as connection:
        for row in connection.execute(
            "SELECT * FROM sources WHERE run_id=? ORDER BY id", (run.id,)
        ):
            meta = SourceMetadata.model_validate_json(row["metadata_json"])
            prefix = f"sources/{meta.source_id}/"
            assert (
                meta.file_path == prefix + "body.txt" and meta.index_path == prefix + "index.json"
            )
            root = env.settings.data_dir / "runs" / run.id
            sources.append(
                {
                    "source_id": meta.source_id,
                    "file_path": meta.file_path,
                    "body_hash": digest((root / meta.file_path).read_bytes()),
                    "index_hash": digest((root / meta.index_path).read_bytes()),
                    "index": json.loads(row["index_json"]),
                }
            )
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run.id,)
            ).fetchone()[0]
            for table in ("briefs", "delivered_history", "mail_outbox")
        }
    return {
        "run_id": run.id,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "deadline_at": run.deadline_at.isoformat(),
        "budget_usage": run.budget_usage.model_dump(),
        "sources": sources,
        "counts": counts,
        "status": run.status,
        "stop_reason": run.stop_reason,
    }


def paired(env, world):
    messages = env.conversations.messages(env.run.id, env.run.user_id)
    ModelRequest(messages=messages, tools=tuple(world.registry.schemas()))
    expected = [call.id for message in messages for call in getattr(message, "tool_calls", ())]
    actual = [message.tool_call_id for message in messages if message.role == "tool"]
    assert expected == actual and len(actual) == len(set(actual))


def record(env, world, scenario, **values):
    events, cursor = [], 0
    store = TimelineStore(env.runs)
    while True:
        page = store.page(env.run.id, env.run.user_id, after_id=cursor, limit=100)
        events.extend(event.model_dump(mode="json") for event in page.events)
        if not page.has_more:
            break
        cursor = page.next_after_id
    report = {
        "scenario": scenario,
        "scope": "stage_three_fixture_quality_and_recovery",
        "fixture_news": True,
        "fixture_models": True,
        "smtp_attempts": 0,
        "state": state(env),
        "timeline": events,
        **values,
    }
    redactor = Redactor(env.settings)
    (env.settings.data_dir / "report.json").write_text(
        json.dumps(redactor.clean(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (env.settings.data_dir / "protocol.json").write_text(
        json.dumps(
            redactor.clean(
                [
                    message.model_dump(mode="json")
                    for message in env.conversations.messages(env.run.id, env.run.user_id)
                ]
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
