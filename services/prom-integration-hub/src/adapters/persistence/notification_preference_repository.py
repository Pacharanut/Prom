"""Postgres-backed NotificationPreferenceRepository."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
        """Select-then-insert inside a SAVEPOINT; if a concurrent toggle inserted the same
        (identity, event_type) row first, the unique constraint fires, the SAVEPOINT rolls back
        alone, and we update the row that won instead (Phase 2B — previously flagged in the
        README as the one repository missing this pattern)."""
        model = await self._get(linked_identity_id, event_type)
        if model is not None:
            model.enabled = enabled
            await self._session.flush()
            return
        try:
            async with self._session.begin_nested():
                self._session.add(
                    NotificationPreferenceModel(
                        id=uuid4(),
                        customer_id=customer_id,
                        linked_identity_id=linked_identity_id,
                        event_type=event_type.value,
                        enabled=enabled,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            winner = await self._get(linked_identity_id, event_type)
            if winner is None:
                raise
            winner.enabled = enabled
            await self._session.flush()

    async def list_disabled(self, linked_identity_id: UUID) -> set[NotificationEventType]:
        stmt = select(NotificationPreferenceModel.event_type).where(
            NotificationPreferenceModel.linked_identity_id == linked_identity_id,
            NotificationPreferenceModel.enabled.is_(False),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {NotificationEventType(value) for value in rows}

    async def _get(
        self, linked_identity_id: UUID, event_type: NotificationEventType
    ) -> NotificationPreferenceModel | None:
        stmt = select(NotificationPreferenceModel).where(
            NotificationPreferenceModel.linked_identity_id == linked_identity_id,
            NotificationPreferenceModel.event_type == event_type.value,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
