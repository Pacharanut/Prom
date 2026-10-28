"""Redis-backed FocusStore — remembers the work item a user was last talking about, so the AI
assistant can resolve "task นี้" / "mark complete" without the user naming it again."""

from __future__ import annotations

import json
from uuid import UUID

from redis.asyncio import Redis

from src.application.ai.ports import FocusStore, WorkItemFocus


def _key(channel_installation_id: UUID, channel_user_id: str) -> str:
    return f"ai-focus:{channel_installation_id}:{channel_user_id}"


class RedisFocusStore(FocusStore):
    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client

    async def get(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> WorkItemFocus | None:
        raw = await self._redis.get(_key(channel_installation_id, channel_user_id))
        if raw is None:
            return None
        payload = json.loads(raw)
        return WorkItemFocus(
            project_id=payload["project_id"],
            work_item_id=payload["work_item_id"],
            label=payload.get("label", ""),
        )

    async def set(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        focus: WorkItemFocus,
        ttl_seconds: int,
    ) -> None:
        payload = json.dumps(
            {
                "project_id": focus.project_id,
                "work_item_id": focus.work_item_id,
                "label": focus.label,
            }
        )
        await self._redis.set(
            _key(channel_installation_id, channel_user_id), payload, ex=ttl_seconds
        )
