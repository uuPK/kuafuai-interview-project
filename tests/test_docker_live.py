"""Opt-in checks against the real local Linux Docker engine and built tool image."""

import asyncio
import json
import os
import subprocess

import pytest

from app.tools.bash import DockerRunner, container_name, register_bash
from app.tools.models import ToolFailure
from tests.test_files import context

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_DOCKER_LIVE") != "1",
    reason="set NEWS_DOCKER_LIVE=1 for real Docker checks",
)


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout


def absent(ctx):
    return not docker(
        "ps", "-a", "--filter", "name=^/" + container_name(ctx) + "$", "--format", "{{.ID}}"
    ).strip()


def test_real_five_tools_and_all_bash_commands(material_env):
    async def run():
        env = material_env
        register_bash(env.registry, env.materials, env.settings)
        assert (await env.invoke("write_file", path="scratch/中文 a", content="AI🙂\nAI🙂\n")).ok
        assert (await env.invoke("list_dir", path="scratch")).ok
        assert (await env.invoke("search_content", keyword="AI🙂")).ok
        assert (await env.invoke("read_file", path="scratch/中文 a")).ok
        commands = [
            "ls scratch",
            'wc -m "scratch/中文 a"',
            'head -n 1 "scratch/中文 a"',
            'tail -n 1 "scratch/中文 a"',
            'sort -u "scratch/中文 a"',
            'uniq -c "scratch/中文 a"',
            'rg -n "AI🙂" scratch',
        ]
        for index, command in enumerate(commands):
            call_id = f"real-{index}"
            result = await env.invoke("bash", call_id=call_id, command=command)
            assert result.ok, result
            assert result.data["stdout"] and result.data["exit_code"] == 0
            assert absent(context(env, call_id))
        assert not list((env.settings.data_dir / "tool_runtime").iterdir())

    asyncio.run(run())


def test_real_readonly_user_network_and_credentials(material_env, monkeypatch):
    async def run():
        env = material_env
        env.seed("scratch/a", "AI")
        for name in (
            "DEEPSEEK_API_KEY",
            "BOCHA_API_KEY",
            "SMTP_PASSWORD",
            "HTTP_PROXY",
            "HTTPS_PROXY",
        ):
            monkeypatch.setenv(name, "fixture-container-credential")
        # Trusted acceptance probe only; this script is never exposed by the model tool schema.
        script = (
            'test "$(id -u)" = 10001 && '
            "! touch /work/scratch/forbidden 2>/dev/null && "
            "! touch /tmp/forbidden 2>/dev/null && "
            "test ! -e /var/run/docker.sock && test ! -e /run/docker.sock && "
            '! env | grep -E "DEEPSEEK|BOCHA|SMTP|PROXY|fixture-container-credential" && '
            '! timeout 2 bash -c "echo >/dev/tcp/1.1.1.1/443" 2>/dev/null && '
            'grep -q "NoNewPrivs:[[:space:]]*1" /proc/self/status && '
            'grep -q "CapEff:[[:space:]]*0000000000000000" /proc/self/status && echo verified'
        )
        ctx = context(env, "isolation")
        code, out, err = await DockerRunner(env.settings).execute(ctx, ["/bin/bash", "-c", script])
        assert (code, out.strip(), err) == (0, "verified", "")
        assert not (env.root / "scratch/forbidden").exists() and absent(ctx)

    asyncio.run(run())


def test_real_container_resource_configuration_and_cancel_cleanup(material_env):
    async def run():
        env = material_env
        env.seed("scratch/a", "AI")
        ctx = context(env, "cancel")
        task = asyncio.create_task(
            DockerRunner(env.settings).execute(ctx, ["/usr/bin/sleep", "20"])
        )
        try:
            found = None
            for _ in range(50):
                if task.done():
                    pytest.fail(f"container exited before inspection: {task.result()}")
                rows = await asyncio.to_thread(
                    docker,
                    "ps",
                    "--filter",
                    "name=^/" + container_name(ctx) + "$",
                    "--format",
                    "{{.ID}}",
                )
                if rows.strip():
                    found = json.loads(await asyncio.to_thread(docker, "inspect", rows.strip()))[0]
                    break
                await asyncio.sleep(0.1)
            assert found is not None
            config = found["HostConfig"]
            assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"]
            assert config["Memory"] == config["MemorySwap"] == 64 * 1024 * 1024
            assert config["NanoCpus"] == 500_000_000 and config["PidsLimit"] == 32
            assert config["CapDrop"] == ["ALL"] and not config["Privileged"]
            assert "no-new-privileges=true" in config["SecurityOpt"]
            assert found["Config"]["User"] == "10001:10001"
            assert len(found["Mounts"]) == 1 and not found["Mounts"][0]["RW"]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert absent(ctx)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["timeout", "output"])
def test_real_timeout_and_output_limit_cleanup(material_env, mode):
    async def run():
        env = material_env
        env.seed("scratch/a", "AI")
        config = env.settings.model_copy(
            update={"bash_timeout_seconds": 1, "bash_output_bytes": 256}
        )
        ctx = context(env, mode)
        argv = (
            ["/usr/bin/sleep", "20"]
            if mode == "timeout"
            else ["/usr/bin/head", "-c", "20000", "/dev/zero"]
        )
        with pytest.raises(
            ToolFailure, match="BASH_TIMEOUT" if mode == "timeout" else "BASH_OUTPUT_LIMIT"
        ):
            await DockerRunner(config).execute(ctx, argv)
        assert absent(ctx)
        assert not list((env.settings.data_dir / "tool_runtime").iterdir())

    asyncio.run(run())
