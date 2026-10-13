"""Keep tests independent of machine credentials and the local .env file."""

import json
from types import SimpleNamespace

import pytest

from app.agent.prompts import initial_messages
from app.agent.protocol import AssistantMessage, FunctionCall, ModelResponse, ModelToolCall
from app.agent.runner import AgentRunner
from app.config import Settings
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.materials import MaterialStore
from app.storage.models import RunRequest, StateChange, SubscriptionSnapshot
from app.storage.runs import RunStore
from app.storage.sources import SourceStore
from app.tools.executor import ToolExecutor
from app.tools.files import register_file_tools
from app.tools.models import ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.stubs import build_stub_registry, register_submission_fixture


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)


@pytest.fixture
def tool_env(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, deepseek_api_key="fixture-deep-key")
    database = Database(settings.database_path)
    database.initialize()
    runs = RunStore(database, settings)
    run = runs.create_run(
        RunRequest(
            user_id="user-1",
            subscription_id="sub-1",
            subscription=SubscriptionSnapshot(
                nickname="研究员", email="reader@example.com", topics=("开源模型",)
            ),
        )
    )
    run = runs.transition(run.id, run.user_id, StateChange(status="running"))
    calls = CallStore(runs)
    registry = build_stub_registry()
    return SimpleNamespace(
        settings=settings,
        database=database,
        runs=runs,
        run=run,
        calls=calls,
        registry=registry,
        executor=ToolExecutor(registry, calls, settings),
    )


@pytest.fixture
def response_factory():
    def make(*calls, content=None, reasoning=None, **metadata):
        return ModelResponse(
            message=AssistantMessage(
                content=content,
                reasoning_content=reasoning,
                tool_calls=tuple(
                    ModelToolCall(
                        id=call_id,
                        function=FunctionCall(
                            name=name,
                            arguments=arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments, ensure_ascii=False),
                        ),
                    )
                    for call_id, name, arguments in calls
                ),
            ),
            **{"finish_reason": "tool_calls" if calls else "stop", **metadata},
        )

    return make


@pytest.fixture
def material_env(tool_env):
    env = tool_env
    env.materials = MaterialStore(
        SourceStore(CandidateStore(env.calls), env.settings), env.settings
    )
    env.registry = ToolRegistry()
    register_file_tools(env.registry, env.materials, env.settings)
    env.executor = ToolExecutor(env.registry, env.calls, env.settings)
    env.root = env.settings.data_dir / "runs" / env.run.id
    env.sequence = 0

    async def invoke(name, *, call_id=None, **arguments):
        env.sequence += 1
        invocation = ToolInvocation(
            tool_call_id=call_id or f"material-{env.sequence}",
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
        return await env.executor.execute(env.run.id, env.run.user_id, invocation)

    def seed(path, text):
        target = env.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        return target

    env.invoke, env.seed = invoke, seed
    return env


@pytest.fixture
def agent_env(tool_env):
    register_submission_fixture(tool_env.registry)
    tool_env.conversations = ConversationStore(tool_env.calls)
    tool_env.conversations.initialize(
        tool_env.run.id, tool_env.run.user_id, initial_messages(tool_env.run)
    )
    tool_env.runner = lambda model, settings=None: AgentRunner(
        model, tool_env.executor, tool_env.conversations, settings or tool_env.settings
    )
    return tool_env
