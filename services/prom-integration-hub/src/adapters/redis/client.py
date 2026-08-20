"""Thin async Redis client wrapper, built from application settings."""

from __future__ import annotations

from functools import lru_cache
from typing import cast

from redis.asyncio import Redis

from src.infrastructure.configuration.settings import get_settings


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    """Return a process-wide cached async Redis client."""
    settings = get_settings()
    return cast(Redis, Redis.from_url(settings.redis_url))
