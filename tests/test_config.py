from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ServiceConfigurationError, Settings


def test_defaults_allow_missing_live_credentials(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    assert settings.max_model_requests == 15
    assert settings.max_search_calls == 8
    assert settings.max_fetch_calls == 12
    assert settings.max_tool_calls == 50
    assert settings.run_timeout_seconds == 600
    assert settings.external_max_attempts == 3
    assert settings.database_path == tmp_path / "data" / "news.sqlite3"
    assert not settings.data_dir.exists()
    assert settings.missing_service_fields("deepseek") == [
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
    ]
    assert settings.missing_service_fields("bocha") == ["BOCHA_SEARCH_URL", "BOCHA_API_KEY"]
    assert settings.missing_service_fields("smtp") == ["SMTP_HOST", "SMTP_FROM"]
    assert settings.missing_service_fields("session") == ["SESSION_SECRET"]


def test_env_file_loading_precedence_and_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        "APP_NAME=新闻测试\nMAX_MODEL_REQUESTS=9\nDEEPSEEK_API_KEY=fixture-key\n"
        "DEEPSEEK_THINKING_ENABLED=true\nSMTP_PORT=2525\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAX_MODEL_REQUESTS", "11")
    settings = Settings(_env_file=env_file)
    assert settings.app_name == "新闻测试"
    assert settings.max_model_requests == 11
    assert settings.deepseek_api_key.get_secret_value() == "fixture-key"
    assert settings.deepseek_thinking_enabled is True
    assert settings.smtp_port == 2525
    assert Settings(_env_file=env_file, max_model_requests=13).max_model_requests == 13


def test_default_dotenv_is_used_by_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MAX_SEARCH_CALLS=4\n", encoding="utf-8")
    assert Settings().max_search_calls == 4


def test_example_config_is_usable_without_real_credentials() -> None:
    settings = Settings(_env_file=Path(__file__).parents[1] / ".env.example")
    assert settings.deepseek_api_key is None
    assert settings.deepseek_base_url is None
    assert settings.bocha_api_key is None
    assert settings.session_secret is None
    assert settings.max_tool_calls == 50


@pytest.mark.parametrize(
    "field",
    [
        "deepseek_api_key",
        "bocha_api_key",
        "smtp_password",
        "smtp_username",
        "session_secret",
        "deepseek_base_url",
        "deepseek_model",
        "smtp_from",
    ],
)
def test_whitespace_placeholders_are_missing(field: str) -> None:
    assert getattr(Settings(_env_file=None, **{field: "   "}), field) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_model_requests", 1),
        ("max_search_calls", 0),
        ("max_fetch_calls", -1),
        ("max_tool_calls", 0),
        ("run_timeout_seconds", 0),
        ("external_max_attempts", 4),
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", float("inf")),
        ("retry_base_seconds", -1),
        ("smtp_port", 65536),
        ("smtp_port", 0),
        ("smtp_tls_mode", "invalid"),
        ("deepseek_temperature", 3),
        ("deepseek_temperature", float("nan")),
        ("deepseek_max_tokens", 0),
        ("max_redirects", -1),
        ("max_download_bytes", 0),
        ("max_tool_result_chars", 0),
        ("max_model_input_chars", 0),
        ("session_ttl_seconds", 0),
        ("app_name", ""),
    ],
)
def test_invalid_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://name:fixture-password@example.com/v1",
        "https://example.com/v1?key=fixture-key",
        "https://example.com/v1#fixture-key",
    ],
)
@pytest.mark.parametrize("field", ["deepseek_base_url", "bocha_search_url"])
def test_service_urls_reject_credentials_and_unsupported_schemes(field: str, url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: url})
    assert url not in str(caught.value)
    assert "fixture-password" not in str(caught.value)


def test_credentials_are_excluded_from_display_and_serialization() -> None:
    secrets = {
        "deepseek_api_key": "fixture-deepseek-key",
        "bocha_api_key": "fixture-bocha-key",
        "smtp_username": "fixture-smtp-user",
        "smtp_password": "fixture-smtp-password",
        "session_secret": "fixture-session-" + "x" * 32,
        "smtp_from": "sender@example.com",
        "smtp_test_recipient": "reader@example.com",
        "smtp_host": "smtp.example.com",
        "deepseek_base_url": "https://model.example.com",
        "bocha_search_url": "https://search.example.com",
    }
    settings = Settings(_env_file=None, **secrets)
    for value in secrets.values():
        assert value not in repr(settings)
        assert value not in settings.model_dump_json()
    assert secrets.keys().isdisjoint(settings.model_dump())


def test_validation_error_hides_short_session_secret() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, session_secret="fixture-short-secret")
    assert "fixture-short-secret" not in str(caught.value)
    assert "at least 32" in str(caught.value)


def test_require_service_reports_names_only() -> None:
    settings = Settings(_env_file=None, deepseek_api_key="fixture-secret")
    with pytest.raises(ServiceConfigurationError) as caught:
        settings.require_service_config("deepseek")
    assert str(caught.value) == "deepseek: missing DEEPSEEK_BASE_URL, DEEPSEEK_MODEL"
    assert "fixture-secret" not in str(caught.value)


def test_ready_services_and_local_smtp() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_base_url="https://model.example.com/v1",
        deepseek_api_key="fixture-key",
        deepseek_model="fixture-model",
        bocha_search_url="https://search.example.com",
        bocha_api_key="fixture-key",
        smtp_host="localhost",
        smtp_from="sender@example.com",
        smtp_tls_mode="none",
        session_secret="x" * 32,
    )
    for service in ("deepseek", "bocha", "smtp", "session"):
        settings.require_service_config(service)


@pytest.mark.parametrize(
    "provided,missing",
    [
        ({"smtp_username": "fixture-user"}, "SMTP_PASSWORD"),
        ({"smtp_password": "fixture-password"}, "SMTP_USERNAME"),
    ],
)
def test_partial_smtp_auth_reports_the_missing_half(provided: dict, missing: str) -> None:
    settings = Settings(
        _env_file=None, smtp_host="localhost", smtp_from="sender@example.com", **provided
    )
    assert settings.missing_service_fields("smtp") == [missing]


def test_relative_data_dir_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings(_env_file=None, data_dir="materials").data_dir == tmp_path / "materials"


def test_settings_are_fixed_for_the_process() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.max_tool_calls = 100


def test_misspelled_dotenv_setting_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / "settings.env"
    env_file.write_text("DEEPSEK_API_KEY=fixture-secret\n", encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=env_file)
    assert "fixture-secret" not in str(caught.value)
