"""Redis-backed WebhookDedupStore — SET-if-not-exists with a TTL, the standard atomic
"have I seen this event id before" primitive."""

from __future__ import annotations

from redis.asyncio import Redis

from src.application.webhooks.ports import WebhookDedupStore


class RedisWebhookDedupStore(WebhookDedupStore):
    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client

    async def mark_seen_if_new(self, event_key: str, ttl_seconds: int) -> bool:
        redis_key = f"webhook-seen:{event_key}"
        # SET key value NX EX ttl — atomically sets only if absent; returns True iff it was set
        # (i.e. this is the first time), None/False if the key already existed (duplicate).
        was_set = await self._redis.set(redis_key, "1", nx=True, ex=ttl_seconds)
        return bool(was_set)
