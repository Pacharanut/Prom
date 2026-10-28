"""Notification preference use cases (Phase 2B, AI MVP item 5 "configurable notification").
Default-on, opt-out: a missing row means enabled (see NotificationPreferenceRepository). Used by
the Connected Accounts page (via prom_bridge → /api/v1/identities/{id}/notification-preferences)
and by the "ตั้งค่าแจ้งเตือน" chat panel on LINE/Discord.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.application.account_linking.ports import (
    AuditLogRepository,
    Clock,
    LinkedIdentityRepository,
)
from src.application.notifications.ports import NotificationPreferenceRepository
from src.application.shared.audit_writer import AuditWriter
from src.domain.audit.entities import ResultStatus
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.notifications.entities import NotificationEventType
from src.shared.errors import NotFoundError

CONFIGURABLE_EVENT_TYPES: tuple[NotificationEventType, ...] = (
    NotificationEventType.DAILY_SUMMARY,
    NotificationEventType.ASSIGNED,
    NotificationEventType.UNASSIGNED,
    NotificationEventType.STATE_CHANGED,
    NotificationEventType.COMPLETED,
    NotificationEventType.COMMENT,
    NotificationEventType.DUE_SOON,
    NotificationEventType.DUE_TODAY,
    NotificationEventType.OVERDUE,
)
"""Display order for every settings surface. All NotificationEventType values are listed."""


@dataclass
class NotificationPreferenceView:
    event_type: NotificationEventType
    enabled: bool


async def _active_identity(
    linked_identities: LinkedIdentityRepository, linked_identity_id: UUID
) -> LinkedIdentity:
    identity = await linked_identities.get(linked_identity_id)
    if identity is None or identity.status is not IdentityStatus.ACTIVE:
        raise NotFoundError(f"linked identity {linked_identity_id} not found")
    return identity


async def _views(
    preferences: NotificationPreferenceRepository, linked_identity_id: UUID
) -> list[NotificationPreferenceView]:
    disabled = await preferences.list_disabled(linked_identity_id)
    return [
        NotificationPreferenceView(event_type=t, enabled=t not in disabled)
        for t in CONFIGURABLE_EVENT_TYPES
    ]


@dataclass
class GetNotificationPreferences:
    linked_identities: LinkedIdentityRepository
    notification_preferences: NotificationPreferenceRepository

    async def execute(self, *, linked_identity_id: UUID) -> list[NotificationPreferenceView]:
        await _active_identity(self.linked_identities, linked_identity_id)
        return await _views(self.notification_preferences, linked_identity_id)


@dataclass
class UpdateNotificationPreferences:
    linked_identities: LinkedIdentityRepository
    notification_preferences: NotificationPreferenceRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        linked_identity_id: UUID,
        changes: dict[NotificationEventType, bool],
        correlation_id: str,
    ) -> list[NotificationPreferenceView]:
        identity = await _active_identity(self.linked_identities, linked_identity_id)
        for event_type, enabled in changes.items():
            await self.notification_preferences.set_enabled(
                identity.customer_id, identity.id, event_type, enabled
            )
        if changes:
            await AuditWriter(self.audit_log, self.clock).write(
                customer_id=identity.customer_id,
                prom_instance_id=identity.prom_instance_id,
                installation_id=identity.channel_installation_id,
                channel_type=identity.channel_type,
                channel_user_id=identity.channel_user_id,
                prom_user_id=identity.prom_user_id,
                action="notification_preferences.updated",
                target_type="linked_identity",
                target_id=str(identity.id),
                correlation_id=correlation_id,
                result_status=ResultStatus.SUCCESS,
                error_code=None,
            )
        return await _views(self.notification_preferences, identity.id)
