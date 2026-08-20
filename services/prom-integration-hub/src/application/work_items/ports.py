"""Ports specific to the work_items module. Clock/SecretProvider/AuditLogRepository/
DelegatedCredentialRepository are reused as-is from account_linking.ports (they're generic
enough — see that module's docstring).
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from src.application.work_items.conversation import ConversationState
from src.domain.idempotency.entities import IdempotencyRecord


class IdempotencyRepository(Protocol):
    async def get(
        self, customer_id: UUID, route: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        """A non-expired record for this (customer_id, route, idempotency_key), if any.
        Implementations must only return unexpired records — the Postgres impl filters
        `expires_at > now` in the query, so callers never need to re-check expiry themselves."""
        ...

    async def add(self, record: IdempotencyRecord) -> None: ...


class ConversationStateRepository(Protocol):
    """Keyed by (channel_installation_id, channel_user_id) — channel_installation_id is already
    channel-specific (a LINE installation and a Discord installation never share a UUID), so no
    separate channel_type discriminator is needed in the key. Gate 5 adapter:
    src/adapters/redis/conversation_store.py, Redis-backed with a TTL (short-lived conversational
    context, not durable state — matches the Plane_Line prototype's reference pattern of a 10-
    minute conversation TTL, reused here as a sensible default, not copied code)."""

    async def get(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> ConversationState | None: ...

    async def set(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        state: ConversationState,
        ttl_seconds: int,
    ) -> None: ...

    async def clear(self, channel_installation_id: UUID, channel_user_id: str) -> None: ...
