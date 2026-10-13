"""Shared discovery contracts; links are candidates, not verified source evidence."""

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from app.tools.models import ToolParameters


def domain_name(value: str) -> str:
    host = value.rstrip(".").lower().encode("idna").decode("ascii")
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise ValueError("domain must be a hostname")
    if host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("domain must be public")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("domain must not be an IP address")


def candidate_url(value: str) -> str:
    """Syntactic validation only. DNS/connection/redirect checks happen when fetching."""
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("invalid candidate URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("candidate URL must use HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("candidate URL must not contain credentials")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = domain_name(host)
    else:
        if not address.is_global:
            raise ValueError("candidate URL must not use a private address")
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    port = parsed.port  # Also rejects malformed/out-of-range ports.
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


def matches_domains(url: str, domains: tuple[str, ...]) -> bool:
    host = urlsplit(url).hostname
    return not domains or any(host == domain or host.endswith("." + domain) for domain in domains)


class SearchParameters(ToolParameters):
    query: str = Field(min_length=1, max_length=300)
    language: Literal["zh", "en"] = "zh"
    domains: tuple[str, ...] = Field(default=(), max_length=10)
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("query must contain searchable text")
        return value

    @field_validator("domains")
    @classmethod
    def validate_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(domain_name(value) for value in values))

    @property
    def provider_query(self) -> str:
        if not self.domains:
            return self.query
        condition = " OR ".join(f"site:{domain}" for domain in self.domains)
        return f"{self.query} ({condition})"


class CandidateMaterial(ToolParameters):
    title: str = Field(min_length=1, max_length=2000)
    url: str = Field(min_length=1, max_length=4096)
    snippet: str | None = Field(default=None, repr=False)
    summary: str | None = Field(default=None, repr=False)
    site_name: str | None = None
    language: str | None = None
    date_clues: dict[str, str | None] = Field(default_factory=dict)
    discovery_method: Literal["web_search", "read_feed"] = "web_search"
    feed_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")

    @model_validator(mode="after")
    def validate_provenance(self):
        if (self.discovery_method == "read_feed") != (self.feed_id is not None):
            raise ValueError("feed provenance requires method and ID together")
        return self

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return candidate_url(value)
