"""Keep tests independent of machine credentials and the local .env file."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.storage.calls import CallStore
from app.storage.database import Database
from app.storage.models import RunRequest, StateChange, SubscriptionSnapshot
from app.storage.runs import RunStore
from app.tools.executor import ToolExecutor
from app.tools.stubs import build_stub_registry


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
