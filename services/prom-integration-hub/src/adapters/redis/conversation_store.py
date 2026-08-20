"""Redis-backed ConversationStateRepository — short-lived per-(installation, channel_user) wizard
state for the create/update work-item flows.
"""

from __future__ import annotations

import json
from uuid import UUID

from redis.asyncio import Redis

from src.application.work_items.conversation import ConversationState
from src.application.work_items.ports import ConversationStateRepository


def _key(channel_installation_id: UUID, channel_user_id: str) -> str:
    return f"conversation:{channel_installation_id}:{channel_user_id}"


class RedisConversationStateRepository(ConversationStateRepository):
    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client

    async def get(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> ConversationState | None:
        raw = await self._redis.get(_key(channel_installation_id, channel_user_id))
        if raw is None:
            return None
        payload = json.loads(raw)
        return ConversationState(flow=payload["flow"], step=payload["step"], data=payload["data"])

    async def set(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        state: ConversationState,
        ttl_seconds: int,
    ) -> None:
        payload = json.dumps({"flow": state.flow, "step": state.step, "data": state.data})
        await self._redis.set(
            _key(channel_installation_id, channel_user_id), payload, ex=ttl_seconds
        )

    async def clear(self, channel_installation_id: UUID, channel_user_id: str) -> None:
        await self._redis.delete(_key(channel_installation_id, channel_user_id))
