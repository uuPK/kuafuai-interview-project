"""Opt-in real container takeover, with a different task's container preserved."""

import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.stubs import ScriptedModel
from app.harness.execution import execution_scope
from app.tools.bash import DockerRunner, container_name, register_bash
from app.tools.context import bind_context
from app.tools.stubs import register_submission_fixture
from tests.test_agent_runner import submit
from tests.test_recovery import attach, make_worker
from tests.test_worker import claim, start_call

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_DOCKER_LIVE") != "1",
    reason="requires NEWS_DOCKER_LIVE=1 and a running local Linux engine",
)


def test_real_worker_removes_only_the_recovered_runs_containers(material_env, response_factory):
    async def run():
        env = attach(material_env)
        env.seed("scratch/a", "AI")
        register_bash(env.registry, env.materials, env.settings)
        register_submission_fixture(env.registry)
        start_call(env, response_factory(("bash", "bash", {"command": "wc -m scratch/a"})))
        leases, old = claim(env)
        docker = DockerRunner(env.settings)
        runtime = env.settings.data_dir / "tool_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        identifiers = []
        with tempfile.TemporaryDirectory(dir=runtime) as temporary:
            config = Path(temporary)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}', encoding="utf-8")
            try:
                with execution_scope(leases.execution(old)):
                    context = bind_context(
                        env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="bash"
                    )
                    other = replace(context, run_id=str(uuid4()), execution_token=None)
                    # Trusted probe only: the model-facing Bash grammar still cannot run sleep.
                    for target in (context, other):
                        prefix, arguments = docker.arguments(
                            target, ["/usr/bin/sleep", "60"], container_name(target), config
                        )
                        arguments.insert(arguments.index("run") + 1, "-d")
                        code, out, err = await docker.cli(arguments, timeout=8, maximum=4096)
                        assert code == 0, err
                        identifiers.append(out.strip())
                    with env.database.transaction() as connection:
                        env.runs._append_event(
                            connection,
                            env.run.id,
                            "bash_started",
                            env.runs.now(),
                            {"tool_call_id": "bash", "container_name": container_name(context)},
                        )
                env.clock.advance(31)
                done = await make_worker(
                    env,
                    ScriptedModel([submit(response_factory)]),
                    materials=env.materials,
                    docker=docker,
                ).run_once()
                assert done.status == "completed"
                for identifier, present in zip(identifiers, (False, True), strict=True):
                    code, out, _ = await docker.cli(
                        [
                            *prefix,
                            "ps",
                            "-a",
                            "--filter",
                            "id=" + identifier,
                            "--format",
                            "{{.ID}}",
                        ],
                        timeout=3,
                        maximum=4096,
                    )
                    assert code == 0 and bool(out.strip()) is present
                assert env.calls.get_call(env.run.id, "user-1", "bash").result.ok
            finally:
                if identifiers:
                    await docker.cli([*prefix, "rm", "-f", *identifiers], timeout=3, maximum=4096)

    asyncio.run(run())
