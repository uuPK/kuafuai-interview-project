from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_is_public_and_contains_no_configuration(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", deepseek_api_key="fixture-key")
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "fixture-key" not in response.text
    assert not settings.data_dir.exists()


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
