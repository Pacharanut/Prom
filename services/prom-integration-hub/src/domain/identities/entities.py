"""LinkedIdentity — the durable mapping between a channel end-user (LINE/Discord) and a Prom
user. LinkingCode is the short-lived one-time code that establishes that mapping.
DelegatedCredential is the encrypted Prom API token acting on behalf of a Prom user, keyed by
(prom_instance_id, prom_user_id) and shared across all of that user's linked channel identities
on that instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from src.domain.integrations.entities import ChannelType


class IdentityStatus(StrEnum):
    ACTIVE = "active"
    UNLINKED = "unlinked"


class InvalidLinkingCodeFormatError(ValueError):
    """Raised when a LinkingCode is constructed with a code that is not exactly 6 digits."""


@dataclass
class LinkedIdentity:
    id: UUID
    customer_id: UUID
    channel_installation_id: UUID
    channel_type: ChannelType
    channel_user_id: str
    prom_instance_id: UUID
    prom_user_id: str
    default_workspace_connection_id: UUID | None
    status: IdentityStatus
    linked_at: datetime
    unlinked_at: datetime | None


@dataclass
class LinkingCode:
    id: UUID
    customer_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    channel_installation_id: UUID
    code: str
    default_workspace_connection_id: UUID | None
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        if not (len(self.code) == 6 and self.code.isdigit()):
            raise InvalidLinkingCodeFormatError(
                f"code must be exactly 6 digits, got: {self.code!r}"
            )

    def is_valid_at(self, now: datetime) -> bool:
        return self.used_at is None and self.expires_at > now


@dataclass
class DelegatedCredential:
    id: UUID
    customer_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    encrypted_token: bytes
    created_at: datetime
    updated_at: datetime
    rotated_at: datetime | None
