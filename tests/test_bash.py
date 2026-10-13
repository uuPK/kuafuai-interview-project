import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.tools.bash import CommandPolicy, DockerRunner, container_name, register_bash
from app.tools.files import FileAccess
from app.tools.models import ToolFailure
from tests.test_files import context


class Runner:
    def __init__(self, output=(0, "2 files\n", "")):
        self.output, self.seen = output, []

    async def execute(self, ctx, argv):
        self.seen.append((ctx, argv))
        return self.output


@pytest.mark.parametrize(
    "command,expected",
    [
        ("ls", ["/usr/bin/ls", "--", "/work"]),
        ("ls -la scratch", ["/usr/bin/ls", "-la", "--", "/work/scratch"]),
        ('wc -m "scratch/中文 a"', ["/usr/bin/wc", "-m", "--", "/work/scratch/中文 a"]),
        ('head -n 2 "scratch/中文 a"', ["/usr/bin/head", "-n", "2", "--", "/work/scratch/中文 a"]),
        (
            'tail -c 6000 "scratch/中文 a"',
            ["/usr/bin/tail", "-c", "6000", "--", "/work/scratch/中文 a"],
        ),
        ('sort -u "scratch/中文 a"', ["/usr/bin/sort", "-u", "--", "/work/scratch/中文 a"]),
        ('uniq -c "scratch/中文 a"', ["/usr/bin/uniq", "-c", "--", "/work/scratch/中文 a"]),
        (
            'rg -n "C++ [AI]" scratch',
            [
                "/usr/bin/rg",
                "-n",
                "--no-config",
                "--fixed-strings",
                "--",
                "C++ [AI]",
                "/work/scratch",
            ],
        ),
    ],
)
def test_allowed_commands_are_separate_argv(material_env, command, expected):
    env = material_env
    env.seed("scratch/中文 a", "C++ [AI]\n")
    policy = CommandPolicy(FileAccess(env.materials, env.settings))
    name, argv = policy.parse(context(env), command)
    assert name == expected[0].split("/")[-1] and argv == expected


@pytest.mark.parametrize(
    "command",
    [
        "rm scratch/a",
        "python scratch/a",
        "sh scratch/a",
        "cat scratch/a",
        "env",
        "",
        "ls | wc -l",
        "ls; ls",
        "ls && ls",
        "ls > scratch/a",
        "ls < scratch/a",
        "ls `id`",
        "ls $(id)",
        "ls\nls",
        "ls $HOME",
        'ls "scratch\\a"',
        'ls "unfinished',
        "rg --pre=python AI scratch",
        "rg --files scratch",
        "sort -o scratch/out scratch/a",
        "head -n 0 scratch/a",
        "head -n 201 scratch/a",
        "tail -c 6001 scratch/a",
        "head -n -1 scratch/a",
        "head -n ① scratch/a",
        "wc",
        "wc scratch",
        "uniq scratch/a scratch/b",
        "ls /etc",
        "ls ../",
        "ls C:/",
        "ls scratch/.env",
        "ls scratch/a:stream",
        "rg '' scratch",
        "rg",
        "ls fixture-deep-key",
        "ls scratch/missing",
    ],
)
def test_unsafe_commands_never_reach_runner(material_env, command):
    env, runner = material_env, Runner()
    env.seed("scratch/a", "AI")
    env.seed("scratch/b", "AI")
    register_bash(env.registry, env.materials, env.settings, runner)
    result = asyncio.run(env.invoke("bash", command=command))
    assert not result.ok and not runner.seen
    assert result.error.code in {
        "BASH_COMMAND_BLOCKED",
        "PERMISSION_DENIED",
        "FILE_NOT_FOUND",
        "INVALID_ARGUMENTS",
    }


@pytest.mark.parametrize(
    "code,stderr,expected",
    [
        (0, "", None),
        (1, "", None),
        (2, "error", "BASH_COMMAND_FAILED"),
        (125, "daemon", "BASH_UNAVAILABLE"),
        (1, "failed to connect to engine", "BASH_UNAVAILABLE"),
    ],
)
def test_bash_result_status_trace_and_budget(material_env, code, stderr, expected):
    env, runner = material_env, Runner((code, "fixture-deep-key" + "中文" * 9000, stderr))
    env.seed("scratch/a", "AI")
    register_bash(env.registry, env.materials, env.settings, runner)
    result = asyncio.run(env.invoke("bash", call_id="bash", command="rg unknown scratch"))
    if expected:
        assert result.error.code == expected
    else:
        assert result.ok and result.data["truncated"] and result.data["read_only"]
        assert "fixture-deep-key" not in result.model_dump_json()
        assert len(result.model_dump_json()) <= env.settings.max_tool_result_chars
    assert len(runner.seen) == 1
    events = [
        e for e in env.runs.list_events(env.run.id, env.run.user_id) if e.kind == "bash_started"
    ]
    assert len(events) == 1 and events[0].data["container_name"] == container_name(
        context(env, "bash")
    )
    assert env.runs.get_run(env.run.id, env.run.user_id).budget_usage.tool_calls == 1


@pytest.mark.parametrize(
    "path,text",
    [
        ("scratch/a", "fixture-deep-key"),
        ("scratch/.env", "hidden"),
        ("untrusted/a", "outside material directories"),
    ],
)
def test_entire_mount_checked_before_docker(material_env, path, text):
    env, runner = material_env, Runner()
    env.seed(path, text)
    register_bash(env.registry, env.materials, env.settings, runner)
    result = asyncio.run(env.invoke("bash", command="ls"))
    assert not result.ok and not runner.seen


def test_docker_fixed_security_arguments_and_clean_environment(material_env, monkeypatch):
    env = material_env
    for name in (
        "DEEPSEEK_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "DOCKER_HOST",
        "BASH_ENV",
        "SMTP_PASSWORD",
    ):
        monkeypatch.setenv(name, "credential-value")
    runner = DockerRunner(env.settings)
    prefix, arguments = runner.arguments(
        context(env), ["/usr/bin/ls", "--", "/work"], "owned-name", Path("config")
    )
    assert not any("credential-value" == value for value in runner.environment().values())
    assert {
        "--read-only",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--user=10001:10001",
        "--pids-limit=32",
        "--memory=64m",
        "--memory-swap=64m",
        "--cpus=0.5",
        "--pull=never",
        "--log-driver=none",
    } <= set(arguments)
    assert (
        arguments[arguments.index("--mount") + 1] == f"type=bind,src={env.root},dst=/work,readonly"
    )
    assert arguments.count("--mount") == 1
    assert 'exec "$@"' in arguments and "--noprofile" in arguments and "--norc" in arguments
    assert prefix[:2] == ["docker", "--config"] and "--host" in prefix
    assert not any("docker.sock" in argument for argument in arguments[arguments.index("run") :])


@pytest.mark.parametrize("mode", ["success", "failure", "timeout", "cancel"])
def test_runner_always_removes_owned_container_and_config(material_env, mode):
    async def run():
        env, calls = material_env, []

        class Controlled(DockerRunner):
            async def cli(self, argv, **kwargs):
                directory = Path(argv[2])
                assert json.loads((directory / "config.json").read_text()) == {
                    "auths": {},
                    "proxies": {},
                }
                calls.append(argv)
                if "run" in argv:
                    if mode == "failure":
                        raise ToolFailure("BASH_OUTPUT_LIMIT", "oversized")
                    if mode == "timeout":
                        raise ToolFailure("BASH_TIMEOUT", "timeout")
                    if mode == "cancel":
                        raise asyncio.CancelledError
                    return 0, "ok", ""
                return 0, "", ""

        runner = Controlled(env.settings)
        if mode == "success":
            assert await runner.execute(context(env), ["/usr/bin/ls", "--", "/work"]) == (
                0,
                "ok",
                "",
            )
        else:
            with pytest.raises(asyncio.CancelledError if mode == "cancel" else ToolFailure):
                await runner.execute(context(env), ["/usr/bin/ls", "--", "/work"])
        assert calls[-1][-3:] == ["rm", "-f", container_name(context(env))]
        assert len(calls) == 2 and not Path(calls[0][2]).exists()

    asyncio.run(run())


class Process:
    def __init__(self, stdout=b"", stderr=b"", block=False):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.block, self.returncode, self.killed = block, None, False
        self.finished = asyncio.Event()
        if not block:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self):
        if self.block:
            await self.finished.wait()
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.finished.set()
        self.stdout.feed_eof()
        self.stderr.feed_eof()


@pytest.mark.parametrize("mode", ["success", "combined_limit", "timeout", "cancel", "missing"])
def test_subprocess_output_timeout_and_cancel_are_bounded(material_env, mode):
    async def run():
        process = Process(
            b"hello" if mode == "success" else b"x" * 200,
            b"" if mode == "success" else b"y" * 200,
            block=mode != "success",
        )

        async def spawn(*argv, **kwargs):
            assert argv == ("docker", "fixed") and kwargs["stdin"] == asyncio.subprocess.DEVNULL
            if mode == "missing":
                raise FileNotFoundError("no docker")
            return process

        runner = DockerRunner(material_env.settings, spawn=spawn)
        if mode == "success":
            assert await runner.cli(["docker", "fixed"], timeout=1, maximum=300) == (0, "hello", "")
        elif mode == "cancel":
            task = asyncio.create_task(runner.cli(["docker", "fixed"], timeout=1, maximum=1000))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            code = {
                "combined_limit": "BASH_OUTPUT_LIMIT",
                "timeout": "BASH_TIMEOUT",
                "missing": "BASH_UNAVAILABLE",
            }[mode]
            with pytest.raises(ToolFailure, match=code):
                await runner.cli(
                    ["docker", "fixed"],
                    timeout=0.02,
                    maximum=300 if mode == "combined_limit" else 1000,
                )
        if mode in {"combined_limit", "timeout", "cancel"}:
            assert process.killed and process.returncode == -9

    asyncio.run(run())


@pytest.mark.parametrize(
    "fields",
    [
        {"bash_docker_host": "tcp://remote:2375"},
        {"bash_docker_host": "unix:///a\n"},
        {"bash_image": "image --privileged"},
        {"bash_timeout_seconds": 31},
        {"bash_output_bytes": 1},
        {"max_material_files": 0},
        {"max_note_chars": 100001},
    ],
)
def test_bash_and_material_configuration_boundaries(fields):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **fields)
