"""Trusted test child: die after committed mail insertion, before Worker releases its lease."""

import asyncio
import os
import sys
from datetime import datetime

from app.agent.brief_rules import BriefRules
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.storage.briefs import BriefStore
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.research import ResearchStore
from app.storage.runs import RunStore
from app.storage.sources import SourceStore
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.submit import register_submission
from app.worker.recovery import Recovery
from app.worker.service import Worker


async def main():
    config = Settings(
        _env_file=None, data_dir=sys.argv[1], worker_lease_seconds=3, worker_heartbeat_seconds=0.1
    )
    database = Database(config.database_path)
    database.initialize()
    runs = RunStore(database, config, clock=lambda: datetime.fromisoformat(sys.argv[2]))
    calls = CallStore(runs)
    sources = SourceStore(CandidateStore(calls), config)
    briefs = BriefStore(ResearchStore(calls, config), BriefRules(sources, config))
    registry = ToolRegistry()
    register_submission(registry, briefs, review_basis="fixture")

    def after_commit(row):
        if row["kind"] == "mail_queued":
            os._exit(37)

    database.event_sink = after_commit
    runner = AgentRunner(
        ScriptedModel([]), ToolExecutor(registry, calls, config), ConversationStore(calls), config
    )
    await Worker(runner, recovery=Recovery(runner, sources=sources)).run_once()
    raise AssertionError("fixture did not reach the committed mail transaction")


if __name__ == "__main__":
    asyncio.run(main())
