"""MessagePublisher — the outbound counterpart to ChannelCredentialValidator (Gate 3): given
already-decrypted channel credentials, send one private (DM) text message to one channel user.
Framework-free; src/channels/line/ (Gate 5) and a Discord counterpart (Gate 6) implement it.

Gate 7 adds the two repository ports the notification pipeline needs: preferences (mute one
event type per identity) and the outbound-message debounce/retry/dead-letter ledger.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from src.domain.integrations.entities import ChannelType
from src.domain.notifications.entities import NotificationEventType, OutboundMessageRecord


class MessagePublisher(Protocol):
    async def send_private_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        channel_user_id: str,
        text: str,
    ) -> None:
        """Push a private text message to one end user on the given channel. Raises
        ChannelDeliveryFailedError (see src.shared.errors) on failure — callers must not let a
        delivery failure surface as an unhandled exception."""
        ...


class NotificationPreferenceRepository(Protocol):
    async def is_enabled(self, linked_identity_id: UUID, event_type: NotificationEventType) -> bool:
        """True unless an explicit disabled row exists for this (identity, event_type) pair —
        default-on, opt-out model per §8.6."""
        ...

    async def set_enabled(
        self,
        customer_id: UUID,
        linked_identity_id: UUID,
        event_type: NotificationEventType,
        enabled: bool,
    ) -> None:
        """Upsert — set or clear the mute for one event type."""
        ...


class OutboundMessageRepository(Protocol):
    async def get_pending(
        self, linked_identity_id: UUID, work_item_id: str
    ) -> OutboundMessageRecord | None:
        """The current debounce-window buffer record for this (identity, work_item) pair, if
        one is still pending (not yet sent/dead-lettered)."""
        ...

    async def get(self, record_id: UUID) -> OutboundMessageRecord | None: ...
    async def add(self, record: OutboundMessageRecord) -> None: ...
    async def save(self, record: OutboundMessageRecord) -> None: ...
