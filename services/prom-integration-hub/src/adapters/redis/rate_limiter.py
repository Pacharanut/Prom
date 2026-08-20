"""Redis-backed RateLimiter — fixed-window counter via INCR + EXPIRE."""

from __future__ import annotations

from redis.asyncio import Redis

from src.application.account_linking.ports import RateLimiter


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client

    async def check_and_increment(self, key: str, limit: int, window_seconds: int) -> bool:
        redis_key = f"rate-limit:{key}"
        count = int(await self._redis.incr(redis_key))
        if count == 1:
            # Only the attempt that created the counter sets its expiry, so the window is fixed
            # from first-attempt time rather than sliding on every subsequent increment.
            await self._redis.expire(redis_key, window_seconds)
        return count <= limit
