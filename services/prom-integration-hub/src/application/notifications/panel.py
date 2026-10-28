"""NotificationsPanel — what the LINE/Discord handlers call for the "ตั้งค่าแจ้งเตือน" settings
panel and the on-demand "สรุปงาน" summary (Phase 2B). Channel-agnostic, like
src/application/work_items/conversation.py: the handlers only render what comes back.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.application.notifications.preferences import (
    GetNotificationPreferences,
    NotificationPreferenceView,
    UpdateNotificationPreferences,
)
from src.application.notifications.summary import BuildDailySummary, DailySummary
from src.domain.identities.entities import LinkedIdentity
from src.domain.notifications.entities import NotificationEventType


@dataclass
class NotificationsPanel:
    get_preferences: GetNotificationPreferences
    update_preferences: UpdateNotificationPreferences
    build_daily_summary: BuildDailySummary

    async def preferences(self, linked_identity_id: UUID) -> list[NotificationPreferenceView]:
        return await self.get_preferences.execute(linked_identity_id=linked_identity_id)

    async def toggle(
        self, linked_identity_id: UUID, event_type_value: str, correlation_id: str
    ) -> list[NotificationPreferenceView]:
        """Flips one switch. An unknown value (a stale or forged button) changes nothing and
        just returns the current settings."""
        current = await self.preferences(linked_identity_id)
        try:
            event_type = NotificationEventType(event_type_value)
        except ValueError:
            return current
        enabled_now = next((v.enabled for v in current if v.event_type == event_type), None)
        if enabled_now is None:
            return current
        return await self.update_preferences.execute(
            linked_identity_id=linked_identity_id,
            changes={event_type: not enabled_now},
            correlation_id=correlation_id,
        )

    async def summary_now(
        self, identity: LinkedIdentity, workspace_slug: str, correlation_id: str
    ) -> DailySummary:
        return await self.build_daily_summary.execute(
            identity=identity, workspace_slug=workspace_slug, correlation_id=correlation_id
        )
