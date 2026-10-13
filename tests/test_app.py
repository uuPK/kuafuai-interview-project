from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage.database import Database, SchemaVersionError
from app.storage.models import RunRequest, SubscriptionSnapshot


def test_health_is_public_and_contains_no_configuration(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", deepseek_api_key="fixture-key")
    application = create_app(settings)
    assert not settings.data_dir.exists()
    with TestClient(application) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "fixture-key" not in response.text
    assert settings.database_path.is_file()


def test_application_instances_have_independent_settings() -> None:
    first = Settings(_env_file=None, app_name="first")
    second = Settings(_env_file=None, app_name="second")
    assert create_app(first).state.settings is first
    assert create_app(second).state.settings is second


def test_factory_loads_settings_without_an_injected_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APP_NAME=测试应用\n", encoding="utf-8")
    application = create_app()
    assert application.title == "测试应用"
    with TestClient(application) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_startup_keeps_existing_runs_across_application_instances(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    first = create_app(settings)
    with TestClient(first):
        run = first.state.run_store.create_run(
            RunRequest(
                user_id="user-1",
                subscription_id="sub-1",
                subscription=SubscriptionSnapshot(
                    nickname="研究员", email="reader@example.com", topics=("开源模型",)
                ),
            )
        )
    second = create_app(settings)
    with TestClient(second):
        assert second.state.run_store.get_run(run.id, "user-1") == run


def test_incompatible_database_prevents_startup(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    with database.connection() as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(SchemaVersionError), TestClient(create_app(settings)):
        pass


def test_startup_registers_basic_tools_without_starting_docker(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("startup must not run Docker")

    monkeypatch.setattr("app.tools.bash.DockerRunner.execute", forbidden)
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        registry = client.app.state.tool_registry
        assert {schema["function"]["name"] for schema in registry.schemas()} == {
            "web_search",
            "fetch_url",
            "read_feed",
            "list_dir",
            "read_file",
            "search_content",
            "write_file",
            "bash",
        }
        assert client.get("/health").status_code == 200
