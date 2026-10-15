"""One research task at a time, with an independent heartbeat and fail-closed takeover."""

import asyncio
import logging
from uuid import uuid4

from app.harness.execution import LeaseLostError, execution_scope
from app.storage.leases import LeaseStore
from app.tools.models import ToolFailure
from app.worker.recovery import Recovery

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, runner, *, recovery=None, worker_id=None):
        self.runner, self.settings = runner, runner.settings
        self.leases = LeaseStore(runner.runs, self.settings)
        self.recovery = recovery or Recovery(runner)
        if self.recovery.runner is not runner:
            raise ValueError("worker recovery and runner must share their stores")
        self.worker_id = worker_id or uuid4().hex
        self.lock = asyncio.Lock()

    async def _execute(self, lease):
        with execution_scope(self.leases.execution(lease)):
            try:
                await self.recovery.recover(lease.run_id, lease.user_id)
                return await self.runner.run(lease.run_id, lease.user_id)
            except LeaseLostError:
                raise
            except ToolFailure as error:
                reason = (
                    "recovery_cleanup_failed"
                    if error.result.error.code.startswith("BASH_")
                    else "recovery_material_state_problem"
                )
                return await self.recovery.fail_and_pair(
                    lease.run_id,
                    lease.user_id,
                    reason,
                    error.result.error.code,
                )
            except Exception:
                log.error("worker execution failed: run_id=%s", lease.run_id)
                return await self.recovery.fail_and_pair(
                    lease.run_id,
                    lease.user_id,
                    "worker_execution_failed",
                    "WORKER_EXECUTION_FAILED",
                )

    async def _heartbeat(self, lease):
        while True:
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)
            self.leases.heartbeat(lease)

    async def run_once(self):
        async with self.lock:
            self.leases.touch_worker(self.worker_id)
            lease = self.leases.claim_next(self.worker_id)
            if lease is None:
                return None
            task = asyncio.create_task(self._execute(lease))
            pulse = asyncio.create_task(self._heartbeat(lease))
            try:
                done, _ = await asyncio.wait((task, pulse), return_when=asyncio.FIRST_COMPLETED)
                if pulse in done:
                    pulse.result()  # Lease loss or storage failure cancels execution below.
                    raise LeaseLostError("heartbeat stopped")
                return task.result()
            finally:
                cleanup = asyncio.create_task(self._finish(task, pulse, lease))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # A stop signal may arrive while heartbeat failure is already unwinding.
                    await cleanup
                    raise

    async def _finish(self, task, pulse, lease):
        task.cancel()
        pulse.cancel()
        try:
            await asyncio.gather(task, pulse, return_exceptions=True)
        finally:
            self.leases.release(lease)

    async def serve(self, stop=None):
        stop = stop or asyncio.Event()
        try:
            while not stop.is_set():
                try:
                    await self._until_stop(stop)
                except LeaseLostError:
                    log.warning("worker claim lost; execution stopped")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_seconds)
                except TimeoutError:
                    pass
        finally:
            self.leases.stop_worker(self.worker_id)

    async def _until_stop(self, stop):
        execution = asyncio.create_task(self.run_once())
        halted = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((execution, halted), return_when=asyncio.FIRST_COMPLETED)
            if halted not in done:
                return execution.result()
        finally:
            execution.cancel()
            halted.cancel()
            await asyncio.gather(execution, halted, return_exceptions=True)
