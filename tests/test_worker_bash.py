import asyncio
import json
from dataclasses import replace

import pytest

from app.harness.execution import execution_scope
from app.tools.bash import DockerRunner, container_name, register_bash
from app.tools.context import bind_context
from app.tools.models import ToolFailure
from app.tools.stubs import register_submission_fixture
from tests.test_agent_runner import submit
from tests.test_recovery import attach, make_worker
from tests.test_worker import claim, start_call


class CleanupDocker(DockerRunner):
    def __init__(self, settings, context, scenario):
        super().__init__(settings)
        self.context, self.scenario, self.calls = context, scenario, []

    async def cli(self, arguments, **kwargs):
        self.calls.append(arguments)
        config_path = arguments[arguments.index("--config") + 1]
        from pathlib import Path

        assert json.loads((Path(config_path) / "config.json").read_text()) == {
            "auths": {},
            "proxies": {},
        }
        if "ps" in arguments:
            if any(argument.startswith("id=") for argument in arguments):
                return 0, "" if self.scenario.endswith("gone") else "a" * 12, ""
            if self.scenario == "ps_failed":
                return 1, "", ""
            if self.scenario == "bad_list":
                return 0, "untrusted;command", ""
            return 0, "a" * 12 + " news-tool-" + "b" * 24, ""
        if "inspect" in arguments:
            if self.scenario == "inspect_gone":
                return 1, "", ""
            if self.scenario == "bad_json":
                return 0, "invalid", ""
            return (
                0,
                json.dumps(
                    {
                        "daily-ai-news.run": "another"
                        if self.scenario == "foreign"
                        else self.context.run_id
                    }
                ),
                "",
            )
        if "rm" in arguments:
            return (1 if self.scenario in {"rm_failed", "rm_gone"} else 0), "", ""
        raise AssertionError("unexpected Docker command")


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid", None),
        ("ps_failed", "BASH_CLEANUP_UNAVAILABLE"),
        ("bad_list", "BASH_CLEANUP_STATE_PROBLEM"),
        ("foreign", "BASH_CLEANUP_STATE_PROBLEM"),
        ("bad_json", "BASH_CLEANUP_STATE_PROBLEM"),
        ("inspect_gone", None),
        ("rm_failed", "BASH_CLEANUP_UNAVAILABLE"),
        ("rm_gone", None),
    ],
)
def test_cleanup_checks_container_names_labels_and_disappearance(material_env, scenario, expected):
    env = material_env
    context = bind_context(
        env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="bash"
    )
    docker = CleanupDocker(env.settings, context, scenario)
    if expected:
        with pytest.raises(ToolFailure) as error:
            asyncio.run(docker.cleanup_run(context))
        assert error.value.result.error.code == expected
    else:
        assert asyncio.run(docker.cleanup_run(context)) == (1 if scenario == "valid" else 0)
    if scenario in {"bad_list", "foreign", "bad_json", "ps_failed", "inspect_gone"}:
        assert not any("rm" in call for call in docker.calls)


def test_container_names_separate_owner_generations(material_env):
    env = material_env
    context = bind_context(
        env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id="bash"
    )
    first = replace(context, execution_token="a" * 32)
    second = replace(context, execution_token="b" * 32)
    assert container_name(first) != container_name(second) != container_name(context)


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_worker_cleans_before_bash_replay(material_env, response_factory, cleanup_failure):
    async def run():
        env = attach(material_env)
        env.seed("scratch/a", "AI")
        sequence = []

        class Docker:
            async def cleanup_run(self, context):
                sequence.append("clean")
                if cleanup_failure:
                    raise ToolFailure("BASH_CLEANUP_UNAVAILABLE", "fixture cleanup unavailable")
                return 1

            async def execute(self, context, argv):
                assert sequence == ["clean"]
                sequence.append("execute")
                return 0, "2", ""

        docker = Docker()
        register_bash(env.registry, env.materials, env.settings, runner=docker)
        register_submission_fixture(env.registry)
        call, _ = start_call(
            env, response_factory(("bash", "bash", {"command": "wc -m scratch/a"}))
        )
        leases, old = claim(env)
        with execution_scope(leases.execution(old)), env.database.transaction() as connection:
            env.runs._append_event(
                connection,
                env.run.id,
                "bash_started",
                env.runs.now(),
                {"tool_call_id": call.tool_call_id},
            )
        env.clock.advance(31)
        model = ScriptedModel([submit(response_factory)])
        done = await make_worker(env, model, materials=env.materials, docker=docker).run_once()
        assert done.status == ("failed" if cleanup_failure else "completed")
        if cleanup_failure:
            assert sequence == ["clean"] and not model.requests
            assert done.stop_reason == "recovery_cleanup_failed"
        else:
            assert sequence == ["clean", "execute"] and done.budget_usage.tool_calls == 2

    from app.agent.stubs import ScriptedModel

    asyncio.run(run())
