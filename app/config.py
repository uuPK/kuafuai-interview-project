"""Validated process configuration; service credentials are never public output."""

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ServiceName = Literal["deepseek", "bocha", "smtp", "session"]


class ServiceConfigurationError(ValueError):
    """A selected real service is missing configuration (field names only)."""


class Settings(BaseSettings):
    """Read constructor overrides, process environment, .env, then defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="每日 AI 新闻助手", min_length=1, max_length=100)
    data_dir: Path = Path("data")

    deepseek_base_url: AnyHttpUrl | None = Field(default=None, repr=False, exclude=True)
    deepseek_api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    deepseek_model: str | None = Field(default=None, min_length=1, max_length=200)
    deepseek_thinking_enabled: bool = False
    deepseek_reasoning_effort: Literal["low", "high", "max"] = "high"
    deepseek_temperature: float = Field(default=0.2, ge=0, le=2, allow_inf_nan=False)
    deepseek_max_tokens: int = Field(default=4096, ge=1)

    bocha_search_url: AnyHttpUrl | None = Field(default=None, repr=False, exclude=True)
    bocha_api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)

    smtp_host: str | None = Field(default=None, min_length=1, repr=False, exclude=True)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: SecretStr | None = Field(default=None, repr=False, exclude=True)
    smtp_password: SecretStr | None = Field(default=None, repr=False, exclude=True)
    smtp_from: str | None = Field(default=None, min_length=1, repr=False, exclude=True)
    smtp_tls_mode: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_test_recipient: str | None = Field(default=None, min_length=1, repr=False, exclude=True)

    session_secret: SecretStr | None = Field(default=None, repr=False, exclude=True)
    session_cookie_secure: bool = False
    session_ttl_seconds: int = Field(default=86400, ge=60)

    request_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    external_max_attempts: int = Field(default=3, ge=1, le=3)
    retry_base_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    retry_inline_max_seconds: float = Field(default=5, ge=0, le=30, allow_inf_nan=False)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_download_bytes: int = Field(default=2_000_000, ge=1)

    max_model_requests: int = Field(default=15, ge=2)
    max_search_calls: int = Field(default=8, ge=1)
    max_fetch_calls: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=50, ge=1)
    run_timeout_seconds: int = Field(default=600, ge=1)
    # A complete bounded error envelope must fit even at the smallest configured limit.
    max_tool_result_chars: int = Field(default=12_000, ge=256)
    max_model_input_chars: int = Field(default=120_000, ge=1)
    max_model_response_bytes: int = Field(default=2_000_000, ge=256)
    max_review_context_chars: int = Field(default=8000, ge=256, le=50_000)
    worker_lease_seconds: float = Field(default=30, ge=3, le=3600, allow_inf_nan=False)
    worker_heartbeat_seconds: float = Field(default=5, ge=0.1, le=30, allow_inf_nan=False)
    worker_poll_seconds: float = Field(default=1, ge=0.05, le=30, allow_inf_nan=False)
    max_material_file_bytes: int = Field(default=16_100_000, ge=1)
    max_material_scan_bytes: int = Field(default=32_200_000, ge=1)
    max_material_files: int = Field(default=200, ge=1, le=2000)
    max_note_chars: int = Field(default=50_000, ge=1, le=100_000)
    bash_image: str = Field(
        default="daily-ai-news-tools:local",
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+|@sha256:[a-f0-9]{64})$",
    )
    bash_docker_host: str | None = Field(default=None, repr=False, exclude=True)
    bash_timeout_seconds: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)
    bash_output_bytes: int = Field(default=24_000, ge=256, le=1_000_000)
    rss_sources_file: Path | None = Field(default=None, repr=False, exclude=True)
    max_rss_entries: int = Field(default=500, ge=1, le=2000)

    @field_validator("data_dir")
    @classmethod
    def resolve_data_dir(cls, value: Path) -> Path:
        """Resolve the configured storage root without creating runtime files."""
        return value.expanduser().resolve()

    @field_validator("rss_sources_file")
    @classmethod
    def resolve_rss_file(cls, value):
        return value.expanduser().resolve() if value is not None else None

    @field_validator(
        "deepseek_base_url",
        "deepseek_api_key",
        "deepseek_model",
        "bocha_search_url",
        "bocha_api_key",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        "smtp_test_recipient",
        "session_secret",
        mode="before",
    )
    @classmethod
    def empty_service_value(cls, value: object) -> object:
        """Allow empty placeholders while preserving nonempty credential values."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("deepseek_base_url", "bocha_search_url")
    @classmethod
    def validate_service_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("service URL must not contain credentials, query or fragment")
        return value

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("session secret must contain at least 32 characters")
        return value

    @field_validator("bash_docker_host", mode="before")
    @classmethod
    def local_docker_host(cls, value):
        if value is None or value == "":
            return None
        if (
            not isinstance(value, str)
            or not (value.startswith("unix:///") or value.startswith("npipe:////./pipe/"))
            or any(char.isspace() or char in "\x00\r\n" for char in value)
        ):
            raise ValueError("Bash requires a local Docker socket or named pipe")
        return value

    @property
    def database_path(self) -> Path:
        return self.data_dir / "news.sqlite3"

    @model_validator(mode="after")
    def validate_worker_timing(self):
        if self.worker_lease_seconds < 3 * self.worker_heartbeat_seconds:
            raise ValueError("worker lease must allow at least three heartbeat intervals")
        return self

    def missing_service_fields(self, service: ServiceName) -> list[str]:
        """Report names only; missing live credentials do not block stub startup."""
        required = {
            "deepseek": ("deepseek_base_url", "deepseek_api_key", "deepseek_model"),
            "bocha": ("bocha_search_url", "bocha_api_key"),
            "smtp": ("smtp_host", "smtp_from"),
            "session": ("session_secret",),
        }
        names = list(required[service])
        if service == "smtp":
            if self.smtp_username is not None or self.smtp_password is not None:
                names.extend(("smtp_username", "smtp_password"))
        return [name.upper() for name in names if getattr(self, name) is None]

    def require_service_config(self, service: ServiceName) -> None:
        """Real adapters must call this before any external request."""
        missing = self.missing_service_fields(service)
        if missing:
            raise ServiceConfigurationError(f"{service}: missing {', '.join(missing)}")
