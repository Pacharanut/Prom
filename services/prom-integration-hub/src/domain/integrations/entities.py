"""ChannelInstallation — one LINE Official Account or Discord Application/Bot configured for a
customer, and its links to workspace connections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ChannelType(StrEnum):
    LINE = "line"
    DISCORD = "discord"


class InstallationStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    REVOKED = "revoked"


@dataclass
class ChannelInstallation:
    id: UUID
    customer_id: UUID
    channel_type: ChannelType
    display_name: str
    status: InstallationStatus
    external_account_id: str
    encrypted_credentials: bytes
    default_workspace_connection_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass
class InstallationWorkspace:
    id: UUID
    channel_installation_id: UUID
    workspace_connection_id: UUID
    created_at: datetime
