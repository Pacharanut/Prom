"""Typed application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. All fields are required to be explicit — no silent defaults for
    values that affect security or connectivity, except where a safe local-dev default is noted.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")

    database_url: str
    redis_url: str

    celery_broker_url: str
    celery_result_backend: str

    prom_api_base_url: str
    secret_encryption_key: str
    hub_service_token: str
    """Shared secret required on the `X-Hub-Service-Token` header for every /api/v1/* route.
    Models the service-token half of Hybrid Authentication (see AGENTS.md) — these endpoints are
    called by a trusted Prom Core component server-to-server, never by end users, LINE, or
    Discord directly."""

    hub_public_base_url: str = "http://localhost:8000"
    """Gate 7: the externally-reachable base URL of this Hub deployment — used only to compute
    the `webhook_url` shown to an admin in `WorkspaceConnectionResponse` (what to paste into
    Plane's own Webhook settings page). Not used for anything security-sensitive; a wrong value
    here just means the displayed URL is wrong, not a vulnerability."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()  # type: ignore[call-arg]
