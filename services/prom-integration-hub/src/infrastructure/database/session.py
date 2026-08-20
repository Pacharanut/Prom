"""Async session factory and FastAPI dependency for request-scoped sessions."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.infrastructure.database.engine import get_engine


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return a process-wide cached session factory."""
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session.

    Commits on a clean request, rolls back on an exception. `AsyncSession` used as a bare
    `async with` context manager only closes on exit — it does NOT commit — so without this,
    every write made through the real API would be silently rolled back the instant the
    request's session closes, while the HTTP response still reports success. Every existing
    test bypasses this function entirely via `app.dependency_overrides[get_session]`, which is
    why this went undetected until an end-to-end write-then-independently-reread check (Gate 9).
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
