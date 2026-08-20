"""Fixtures for the integration tier — real Postgres + Redis. Assumes `alembic upgrade head` has
already been run against DATABASE_URL (see README "Quality gates" / integration tier section).
Each test runs inside its own transaction, rolled back on teardown, so tests don't need manual
cleanup and can't interfere with each other.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.infrastructure.configuration.settings import get_settings


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Own engine per test, not the app's process-wide cached `get_engine()` singleton.

    `get_engine()` is `@lru_cache`d for the real app's single long-lived event loop. pytest-asyncio
    gives each test function its own event loop, so a cross-test cached engine's connection pool
    ends up bound to a stale loop ("Future attached to a different loop"). A fresh engine per test,
    disposed on teardown, avoids that entirely.
    """
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(get_settings().redis_url)
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
