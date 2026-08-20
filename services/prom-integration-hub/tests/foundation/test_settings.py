"""Settings load correctly from environment variables."""

from __future__ import annotations

from src.infrastructure.configuration.settings import Settings


def test_settings_load_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://host:6379/1")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://host:6379/2")
    monkeypatch.setenv("PROM_API_BASE_URL", "http://plane.example.com")
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "unit-test-key")

    settings = Settings()  # type: ignore[call-arg]

    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"
    assert settings.redis_url == "redis://host:6379/0"
    assert settings.prom_api_base_url == "http://plane.example.com"
    assert settings.environment == "local"
    assert settings.log_level == "INFO"
