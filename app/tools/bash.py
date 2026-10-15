"""A small read-only command grammar executed solely in credential-free Docker containers."""

import asyncio
import json
import os
import re
import shlex
import tempfile
from pathlib import Path

from pydantic import Field

from app.harness.execution import check_current_execution
from app.storage.snapshots import digest
from app.tools.files import FileAccess, bounded_prefix
from app.tools.models import ToolFailure, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition


class BashParameters(ToolParameters):
    command: str = Field(min_length=1, max_length=2000)


def container_name(context):
    if not context.tool_call_id:
        raise PermissionError("Bash requires a server-bound call")
    identity = f"{context.run_id}:{context.tool_call_id}"
    if context.execution_token:
        identity += ":" + context.execution_token
    return "news-tool-" + digest(identity.encode())[:24]


class CommandPolicy:
    FLAGS = {
        "ls": {"-l", "-a", "-la", "-al", "-1", "-d"},
        "wc": {"-c", "-m", "-l", "-w", "-L"},
        "head": set(),
        "tail": set(),
        "sort": {"-u", "-n", "-r"},
        "uniq": {"-c", "-d", "-u"},
        "rg": {"-n", "-i", "-l", "-c", "-F"},
    }

    def __init__(self, access):
        self.access = access

    def parse(self, context, command):
        if self.access.redactor.contains_credential(command):
            raise ToolFailure("BASH_COMMAND_BLOCKED", "命令不能包含敏感凭据。")
        if any(char in "|;&<>`$\\" or ord(char) < 32 for char in command):
            raise ToolFailure(
                "BASH_COMMAND_BLOCKED", "只允许单条白名单命令，不支持 shell 操作符或替换。"
            )
        try:
            words = shlex.split(command)
        except ValueError:
            raise ToolFailure("BASH_COMMAND_BLOCKED", "命令引号或参数格式无效。") from None
        if not words or words[0] not in self.FLAGS:
            raise ToolFailure(
                "BASH_COMMAND_BLOCKED", "仅支持 ls/wc/head/tail/sort/uniq/rg 的安全参数。"
            )
        name, rest = words[0], words[1:]
        flags = []
        while rest and rest[0].startswith("-") and rest[0] != "--":
            flag = rest.pop(0)
            if name in {"head", "tail"} and flag in {"-n", "-c"}:
                if (
                    not rest
                    or not rest[0].isascii()
                    or not rest[0].isdigit()
                    or not 1 <= int(rest[0]) <= (200 if flag == "-n" else 6000)
                ):
                    raise ToolFailure("BASH_COMMAND_BLOCKED", "head/tail 数量超出安全范围。")
                flags.extend((flag, rest.pop(0)))
            elif flag in self.FLAGS[name]:
                flags.append(flag)
            else:
                raise ToolFailure("BASH_COMMAND_BLOCKED", "命令选项未列入允许范围。")
        if rest and rest[0] == "--":
            rest.pop(0)
        pattern = None
        if name == "rg":
            if not rest or not rest[0].strip():
                raise ToolFailure("BASH_COMMAND_BLOCKED", "rg 需要非空字面关键词。")
            pattern = rest.pop(0)
        if not rest:
            if name in {"ls", "rg"}:
                rest = ["."]
            else:
                raise ToolFailure("BASH_COMMAND_BLOCKED", "命令需要当前任务中的文件路径。")
        if len(rest) > 10 or (name == "uniq" and len(rest) != 1):
            raise ToolFailure("BASH_COMMAND_BLOCKED", "文件参数数量超出安全范围。")
        paths = []
        for path in rest:
            target, relative = self.access.resolve(context, path)
            if target.is_dir() or target == context.working_dir:
                if name not in {"ls", "rg"}:
                    raise ToolFailure("BASH_COMMAND_BLOCKED", "此命令只接受文件路径。")
                for material in self.access.walk(context, relative, strict=True):
                    self.access.load(context, material)
            else:
                self.access.load(context, relative)
            paths.append("/work" if relative == "." else "/work/" + relative)
        if name == "rg":
            flags += ["--no-config", "--fixed-strings"]
        return name, [
            "/usr/bin/" + name,
            *flags,
            "--",
            *([pattern] if pattern is not None else []),
            *paths,
        ]


class DockerRunner:
    def __init__(self, settings, *, spawn=asyncio.create_subprocess_exec):
        self.settings, self.spawn = settings, spawn

    def environment(self):
        allowed = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        return {key: value for key, value in os.environ.items() if key in allowed}

    async def cli(self, arguments, *, timeout, maximum):
        process = None
        tasks = []
        total = 0

        async def collect(stream):
            nonlocal total
            output = bytearray()
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return bytes(output)
                total += len(chunk)
                if total > maximum:
                    raise ToolFailure(
                        "BASH_OUTPUT_LIMIT", "命令输出超过限制，请使用 head 或缩小目录。"
                    )
                output.extend(chunk)

        try:
            async with asyncio.timeout(timeout):
                process = await self.spawn(
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self.environment(),
                )
                tasks = [
                    asyncio.create_task(collect(process.stdout)),
                    asyncio.create_task(collect(process.stderr)),
                ]
                stdout, stderr, code = await asyncio.gather(*tasks, process.wait())
                return (
                    code,
                    stdout.decode("utf-8", errors="replace"),
                    stderr.decode("utf-8", errors="replace"),
                )
        except TimeoutError:
            raise ToolFailure("BASH_TIMEOUT", "隔离命令执行超时。", retryable=True) from None
        except OSError:
            raise ToolFailure(
                "BASH_UNAVAILABLE", "Docker 客户端不可用，请检查本地工具环境。"
            ) from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    def arguments(self, context, argv, name, config_dir):
        root = str(context.working_dir)
        if "," in root or '"' in root:
            raise ToolFailure("BASH_CONFIGURATION", "任务目录不适合 Docker bind mount 参数。")
        host = self.settings.bash_docker_host or (
            "npipe:////./pipe/dockerDesktopLinuxEngine"
            if os.name == "nt"
            else "unix:///var/run/docker.sock"
        )
        prefix = ["docker", "--config", str(config_dir), "--host", host]
        arguments = [
            *prefix,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--label",
            "daily-ai-news.run=" + context.run_id,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--user=10001:10001",
            "--pids-limit=32",
            "--memory=64m",
            "--memory-swap=64m",
            "--cpus=0.5",
            "--log-driver=none",
            "--init",
            "--mount",
            f"type=bind,src={root},dst=/work,readonly",
            "--workdir=/work",
            "--env=LANG=C.UTF-8",
            "--env=BASH_ENV=",
            "--env=ENV=",
            "--entrypoint=/bin/bash",
            self.settings.bash_image,
            "--noprofile",
            "--norc",
            "-c",
            'exec "$@"',
            "news-tool",
            *argv,
        ]
        return prefix, arguments

    async def execute(self, context, argv):
        context.assert_current()
        name = container_name(context)
        runtime = self.settings.data_dir / "tool_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime, prefix="docker-") as directory:
            config_dir = Path(directory)
            # Docker's default client config can inject proxy credentials into containers.
            (config_dir / "config.json").write_text('{"auths":{},"proxies":{}}', encoding="utf-8")
            prefix, arguments = self.arguments(context, argv, name, config_dir)
            try:
                check_current_execution()
                return await self.cli(
                    arguments,
                    timeout=self.settings.bash_timeout_seconds,
                    maximum=self.settings.bash_output_bytes,
                )
            finally:
                try:
                    await asyncio.shield(
                        self.cli([*prefix, "rm", "-f", name], timeout=3, maximum=4096)
                    )
                except ToolFailure:
                    pass  # A lost daemon may require Worker cleanup using this deterministic name.

    async def cleanup_run(self, context):
        """Remove only matching run-labelled tool containers before replaying any Bash call."""
        context.assert_current()
        runtime = self.settings.data_dir / "tool_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime, prefix="docker-cleanup-") as directory:
            config = Path(directory)
            (config / "config.json").write_text('{"auths":{},"proxies":{}}', encoding="utf-8")
            prefix, _ = self.arguments(context, [], "unused", config)

            async def absent(identifier):
                code, out, _ = await self.cli(
                    [*prefix, "ps", "-a", "--filter", "id=" + identifier, "--format", "{{.ID}}"],
                    timeout=3,
                    maximum=4096,
                )
                return code == 0 and not out.strip()

            code, out, _ = await self.cli(
                [
                    *prefix,
                    "ps",
                    "-a",
                    "--filter",
                    "label=daily-ai-news.run=" + context.run_id,
                    "--format",
                    "{{.ID}} {{.Names}}",
                ],
                timeout=3,
                maximum=24_000,
            )
            if code != 0:
                raise ToolFailure(
                    "BASH_CLEANUP_UNAVAILABLE", "无法检查残留工具容器，恢复尚未执行。"
                )
            removed = 0
            for line in out.splitlines():
                match = re.fullmatch(r"([a-f0-9]{12,64}) (news-tool-[a-f0-9]{24})", line.strip())
                if match is None:
                    raise ToolFailure("BASH_CLEANUP_STATE_PROBLEM", "容器清单不符合受控工具格式。")
                identifier = match[1]
                code, labels, _ = await self.cli(
                    [*prefix, "inspect", "--format", "{{json .Config.Labels}}", identifier],
                    timeout=3,
                    maximum=4096,
                )
                try:
                    owned = json.loads(labels).get("daily-ai-news.run") == context.run_id
                except (ValueError, AttributeError):
                    owned = False
                if code != 0 and await absent(identifier):
                    continue
                if code != 0 or not owned:
                    raise ToolFailure("BASH_CLEANUP_STATE_PROBLEM", "残留容器归属无法确认。")
                check_current_execution()
                code, _, _ = await self.cli(
                    [*prefix, "rm", "-f", identifier], timeout=3, maximum=4096
                )
                if code != 0 and await absent(identifier):
                    continue
                if code != 0:
                    raise ToolFailure(
                        "BASH_CLEANUP_UNAVAILABLE", "残留工具容器未能清理，恢复尚未执行。"
                    )
                removed += 1
            return removed


class BashTool:
    def __init__(self, store, settings, runner):
        self.store, self.settings, self.runner = store, settings, runner
        self.access = FileAccess(store, settings)
        self.policy = CommandPolicy(self.access)

    async def execute(self, context, parameters):
        name, argv = self.policy.parse(context, parameters.command)
        # Validate every mounted file, including links/hidden entries, before starting Docker.
        total = 0
        for path in self.access.walk(context, ".", strict=True):
            total += self.access.load(context, path).byte_count
            if total > self.settings.max_material_scan_bytes:
                raise ToolFailure("FILE_SCAN_TOO_LARGE", "任务材料总量超过隔离命令检查限制。")
        context.working_dir.mkdir(parents=True, exist_ok=True)
        with self.store.database.transaction() as connection:
            self.store.runs._owned_row(connection, context.run_id, context.user_id)
            self.store.runs._append_event(
                connection,
                context.run_id,
                "bash_started",
                self.store.runs.now(),
                {
                    "tool_call_id": context.tool_call_id,
                    "container_name": container_name(context),
                    "command": name,
                },
            )
        code, stdout, stderr = await self.runner.execute(context, argv)
        unavailable = any(
            phrase in stderr.lower()
            for phrase in (
                "failed to connect",
                "cannot connect",
                "error during connect",
                "error response from daemon",
            )
        )
        if code >= 125 or unavailable:
            raise ToolFailure(
                "BASH_UNAVAILABLE", "Docker 引擎或本地工具镜像不可用，请完成容器联通验收。"
            )
        if code != 0 and not (name == "rg" and code == 1 and not stderr):
            raise ToolFailure("BASH_COMMAND_FAILED", "只读命令未成功完成，请检查文件或调整参数。")
        stdout, stderr = self.access.redactor.clean(stdout), self.access.redactor.clean(stderr)

        def build(count):
            return ToolResult.success(
                {
                    "exit_code": code,
                    "stdout": stdout[:count],
                    "stderr": stderr[: max(0, count - len(stdout))],
                    "truncated": count < len(stdout) + len(stderr),
                    "container_name": container_name(context),
                    "read_only": True,
                }
            )

        return bounded_prefix(
            len(stdout) + len(stderr), build, self.settings.max_tool_result_chars
        )[1]


def register_bash(registry, store, settings, runner=None):
    registry.register(
        ToolDefinition(
            "bash",
            "在无网络、非 root、只读挂载当前任务的 Docker 容器执行"
            "单条 ls/wc/head/tail/sort/uniq/rg 命令，仅允许安全参数。"
            "禁止管道、重定向、替换、任意程序和任务外路径。"
            "rg 使用字面关键词；引用位置请用 search_content/read_file 获取，"
            "Bash 输出不是字符位置索引。",
            BashParameters,
            BashTool(store, settings, runner or DockerRunner(settings)).execute,
            retry_safe=True,
        )
    )
