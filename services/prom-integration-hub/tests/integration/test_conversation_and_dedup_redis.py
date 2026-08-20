"""Integration tests for RedisConversationStateRepository and RedisWebhookDedupStore against a
real Redis instance. Run with: pytest -m integration (requires
docker compose ... up -d redis — see README).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from redis.asyncio import Redis

from src.adapters.redis.conversation_store import RedisConversationStateRepository
from src.adapters.redis.webhook_dedup import RedisWebhookDedupStore
from src.application.work_items.conversation import ConversationState

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_conversation_state_round_trip(redis_client: Redis) -> None:
    repo = RedisConversationStateRepository(redis_client)
    installation_id = uuid4()
    state = ConversationState(flow="create", step="title", data={"title": "Fix bug"})

    assert await repo.get(installation_id, "U1") is None

    await repo.set(installation_id, "U1", state, ttl_seconds=60)
    fetched = await repo.get(installation_id, "U1")

    assert fetched is not None
    assert fetched.flow == "create"
    assert fetched.step == "title"
    assert fetched.data == {"title": "Fix bug"}


@pytest.mark.asyncio
async def test_conversation_state_clear(redis_client: Redis) -> None:
    repo = RedisConversationStateRepository(redis_client)
    installation_id = uuid4()
    await repo.set(
        installation_id,
        "U1",
        ConversationState(flow="update", step="select_action"),
        ttl_seconds=60,
    )

    await repo.clear(installation_id, "U1")

    assert await repo.get(installation_id, "U1") is None


@pytest.mark.asyncio
async def test_conversation_state_is_scoped_per_installation_and_user(redis_client: Redis) -> None:
    repo = RedisConversationStateRepository(redis_client)
    installation_a, installation_b = uuid4(), uuid4()
    await repo.set(
        installation_a, "U1", ConversationState(flow="create", step="title"), ttl_seconds=60
    )

    assert await repo.get(installation_b, "U1") is None
    assert await repo.get(installation_a, "U2") is None
    assert (await repo.get(installation_a, "U1")) is not None


@pytest.mark.asyncio
async def test_webhook_dedup_marks_first_seen_true_and_repeat_false(redis_client: Redis) -> None:
    store = RedisWebhookDedupStore(redis_client)
    key = f"evt-{uuid4()}"

    first = await store.mark_seen_if_new(key, ttl_seconds=60)
    second = await store.mark_seen_if_new(key, ttl_seconds=60)

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_webhook_dedup_different_keys_are_independent(redis_client: Redis) -> None:
    store = RedisWebhookDedupStore(redis_client)

    a = await store.mark_seen_if_new(f"evt-a-{uuid4()}", ttl_seconds=60)
    b = await store.mark_seen_if_new(f"evt-b-{uuid4()}", ttl_seconds=60)

    assert a is True
    assert b is True
