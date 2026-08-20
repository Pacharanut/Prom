"""Postgres-backed NotificationPreferenceRepository."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import NotificationPreferenceModel
from src.application.notifications.ports import NotificationPreferenceRepository
from src.domain.notifications.entities import NotificationEventType


class PostgresNotificationPreferenceRepository(NotificationPreferenceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_enabled(self, linked_identity_id: UUID, event_type: NotificationEventType) -> bool:
        stmt = select(NotificationPreferenceModel.enabled).where(
            NotificationPreferenceModel.linked_identity_id == linked_identity_id,
            NotificationPreferenceModel.event_type == event_type.value,
        )
        enabled = (await self._session.execute(stmt)).scalar_one_or_none()
        return True if enabled is None else bool(enabled)

    async def set_enabled(
        self,
        customer_id: UUID,
        linked_identity_id: UUID,
        event_type: NotificationEventType,
        enabled: bool,
    ) -> None:
        stmt = select(NotificationPreferenceModel).where(
            NotificationPreferenceModel.linked_identity_id == linked_identity_id,
            NotificationPreferenceModel.event_type == event_type.value,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if model is None:
            self._session.add(
                NotificationPreferenceModel(
                    id=uuid4(),
                    customer_id=customer_id,
                    linked_identity_id=linked_identity_id,
                    event_type=event_type.value,
                    enabled=enabled,
                )
            )
        else:
            model.enabled = enabled
        await self._session.flush()
