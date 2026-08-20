"""Integration test for RedisRateLimiter against a real Redis instance. Run with:
pytest -m integration (requires docker compose ... up -d redis — see README).
"""

from __future__ import annotations

import pytest
from redis.asyncio import Redis

from src.adapters.redis.rate_limiter import RedisRateLimiter

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_allows_up_to_limit_then_blocks(redis_client: Redis) -> None:
    limiter = RedisRateLimiter(redis_client)
    key = "test-key"

    results = [await limiter.check_and_increment(key, limit=3, window_seconds=60) for _ in range(4)]

    assert results == [True, True, True, False]


@pytest.mark.asyncio
async def test_different_keys_are_independent(redis_client: Redis) -> None:
    limiter = RedisRateLimiter(redis_client)

    allowed_a = await limiter.check_and_increment("key-a", limit=1, window_seconds=60)
    allowed_b = await limiter.check_and_increment("key-b", limit=1, window_seconds=60)

    assert allowed_a is True
    assert allowed_b is True
