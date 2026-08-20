"""Async SQLAlchemy engine, built from application settings."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.infrastructure.configuration.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return a process-wide cached async engine (asyncpg driver)."""
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)
