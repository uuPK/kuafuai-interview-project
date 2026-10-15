"""Trusted live acceptance program, passed to the one-off Worker; never a model tool."""

import asyncio
import json
import sys
from pathlib import Path

from app.config import Settings
from app.runtime import open_runtime
from app.storage.models import RunRequest, StateChange, SubscriptionSnapshot
from app.tools.bash import DockerRunner, container_name
from app.tools.context import bind_context
from app.tools.models import ToolInvocation


async def main():
    async with open_runtime(Settings(_env_file=None)) as runtime:
        owner = sys.argv[1]
        run = runtime.run_store.create_run(
            RunRequest(
                user_id=owner,
                subscription_id="compose-probe",
                subscription=SubscriptionSnapshot(
                    nickname="fixture", email="reader@example.com", topics=("AI",)
                ),
            )
        )
        runtime.run_store.transition(run.id, owner, StateChange(status="running"))
        for name, arguments in (
            ("write_file", {"path": "scratch/probe.txt", "content": "AI\nAI\n"}),
            ("bash", {"command": "wc -l scratch/probe.txt"}),
        ):
            result = await runtime.tool_executor.execute(
                run.id,
                owner,
                ToolInvocation(
                    tool_call_id="probe-" + name,
                    name=name,
                    arguments=json.dumps(arguments),
                ),
            )
            assert result.ok, result.error
        assert "2" in result.data["stdout"]
        context = bind_context(
            runtime.run_store,
            runtime.settings.data_dir,
            run.id,
            owner,
            tool_call_id="compose-isolation",
        )
        runner = DockerRunner(runtime.settings)
        security = (
            'test "$(id -u)" = 10001 && '
            "! touch /work/scratch/forbidden 2>/dev/null && "
            "! touch /tmp/forbidden 2>/dev/null && "
            "test ! -e /var/run/docker.sock && test ! -e /data/news.sqlite3 && "
            '! env | grep -E "DEEPSEEK|BOCHA|SMTP|PROXY|fixture-compose" && '
            '! timeout 2 bash -c "echo >/dev/tcp/1.1.1.1/443" 2>/dev/null && '
            'grep -q "NoNewPrivs:[[:space:]]*1" /proc/self/status && '
            'grep -q "CapEff:[[:space:]]*0000000000000000" /proc/self/status && echo verified'
        )
        code, out, err = await runner.execute(context, ["/bin/bash", "-c", security])
        assert (code, out.strip(), err) == (0, "verified", ""), (code, out, err)
        task = asyncio.create_task(runner.execute(context, ["/usr/bin/sleep", "5"]))
        prefix, _ = runner.arguments(context, [], "unused", Path("/tmp"))
        inspected = None
        try:
            for _ in range(30):
                code, out, _ = await runner.cli(
                    [*prefix, "inspect", container_name(context)], timeout=3, maximum=24000
                )
                if code == 0:
                    inspected = json.loads(out)[0]
                    break
                await asyncio.sleep(0.1)
            assert inspected is not None
            host = inspected["HostConfig"]
            mount = host["Mounts"][0]
            assert mount["VolumeOptions"]["Subpath"] == "runs/" + run.id
            assert mount["ReadOnly"] and len(host["Mounts"]) == 1
            assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
            assert host["Memory"] == 64 * 1024 * 1024 and host["PidsLimit"] == 32
            assert inspected["Config"]["User"] == "10001:10001"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runtime.run_store.transition(
            run.id, owner, StateChange(status="failed", stop_reason="fixture_probe_finished")
        )
        print(
            "COMPOSE_PROBE "
            + json.dumps(
                {
                    "run_id": run.id,
                    "bash": "passed",
                    "isolation": "passed",
                    "subpath": mount["VolumeOptions"]["Subpath"],
                }
            )
        )


asyncio.run(main())
