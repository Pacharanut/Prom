"""Ports for group bindings (Phase 2D)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from src.domain.groups.entities import GroupBinding
from src.domain.integrations.entities import ChannelType


class GroupBindingRepository(Protocol):
    async def get_by_group(
        self, channel_installation_id: UUID, group_id: str
    ) -> GroupBinding | None: ...

    async def list_for_workspace(self, workspace_connection_id: UUID) -> list[GroupBinding]: ...

    async def save(self, binding: GroupBinding) -> None:
        """Upsert on (channel_installation_id, group_id) — re-binding a group replaces it."""
        ...

    async def delete(self, binding_id: UUID) -> None: ...


class GroupMessagePublisher(Protocol):
    async def send_group_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        group_id: str,
        text: str,
    ) -> None:
        """Push a text message into a LINE group/room or a Discord channel. Raises
        ChannelDeliveryFailedError on failure."""
        ...
