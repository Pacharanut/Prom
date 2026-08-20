"""Shared pytest fixtures. Sets required env vars for Settings before any app import."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://prom:prom@localhost:55432/prom_hub")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/1")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
os.environ.setdefault("PROM_API_BASE_URL", "http://localhost:8000")
# A real (but test-only, never used outside this suite) Fernet key — FernetSecretProvider
# requires a valid 32-byte urlsafe-base64 key, a placeholder string would fail at construction.
os.environ.setdefault("SECRET_ENCRYPTION_KEY", "D8G16d0wGrDjBz3tZuoImTERI31aS8Ii8EkJYyq6rmI=")
os.environ.setdefault("HUB_SERVICE_TOKEN", "test-only-service-token-do-not-use-in-production")
