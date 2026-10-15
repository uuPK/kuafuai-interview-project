"""Run with python -m app.worker [--once]."""

import argparse
import asyncio

from app.agent.protocol import ModelFailure
from app.agent.runner import AgentRunner
from app.config import Settings
from app.runtime import open_runtime
from app.worker.recovery import Recovery
from app.worker.service import Worker


class UnconfiguredModel:
    async def complete(self, request):
        raise ModelFailure("DEEPSEEK_CONFIGURATION", category="configuration")


async def run(once=False, settings=None):
    async with open_runtime(settings or Settings()) as runtime:
        runner = AgentRunner(
            runtime.model_client or UnconfiguredModel(),
            runtime.tool_executor,
            runtime.conversation_store,
            runtime.settings,
        )

        async def maintenance():
            await asyncio.to_thread(runtime.schedule_store.tick)

        worker = Worker(
            runner,
            maintenance=maintenance,
            recovery=Recovery(
                runner, sources=runtime.source_store, materials=runtime.material_store
            ),
        )
        if once:
            try:
                return await worker.run_once()
            finally:
                worker.leases.stop_worker(worker.worker_id)
        await worker.serve()


def main():
    parser = argparse.ArgumentParser(description="每日AI新闻研究Worker")
    parser.add_argument("--once", action="store_true", help="最多领取并处理一个可运行任务后退出")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
