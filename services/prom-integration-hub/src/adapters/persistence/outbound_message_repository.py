"""Postgres-backed OutboundMessageRepository — the debounce-window buffer + retry/dead-letter
ledger for notification sends. No unique constraint on (linked_identity_id, work_item_id) is
enforced at the DB level (multiple historical rows for the same pair are expected over time, once
each prior one reaches status=sent/dead_letter) — `get_pending` filters to status="pending"
in-query instead of relying on a partial unique index the way LinkedIdentity does.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import OutboundMessageRecordModel
from src.application.notifications.ports import OutboundMessageRepository
from src.domain.notifications.entities import (
    NotificationEventType,
    OutboundMessageRecord,
    OutboundMessageStatus,
)


def _to_domain(model: OutboundMessageRecordModel) -> OutboundMessageRecord:
    batched_types = json.loads(model.event_types_batched)
    return OutboundMessageRecord(
        id=model.id,
        linked_identity_id=model.linked_identity_id,
        work_item_id=model.work_item_id,
        event_types_batched=[NotificationEventType(t) for t in batched_types],
        status=OutboundMessageStatus(model.status),
        attempt_count=model.attempt_count,
        scheduled_for=model.scheduled_for,
        sent_at=model.sent_at,
        created_at=model.created_at,
    )


class PostgresOutboundMessageRepository(OutboundMessageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_pending(
        self, linked_identity_id: UUID, work_item_id: str
    ) -> OutboundMessageRecord | None:
        stmt = select(OutboundMessageRecordModel).where(
            OutboundMessageRecordModel.linked_identity_id == linked_identity_id,
            OutboundMessageRecordModel.work_item_id == work_item_id,
            OutboundMessageRecordModel.status == OutboundMessageStatus.PENDING.value,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get(self, record_id: UUID) -> OutboundMessageRecord | None:
        model = await self._session.get(OutboundMessageRecordModel, record_id)
        return _to_domain(model) if model is not None else None

    async def add(self, record: OutboundMessageRecord) -> None:
        self._session.add(
            OutboundMessageRecordModel(
                id=record.id,
                linked_identity_id=record.linked_identity_id,
                work_item_id=record.work_item_id,
                event_types_batched=json.dumps([t.value for t in record.event_types_batched]),
                status=record.status.value,
                attempt_count=record.attempt_count,
                scheduled_for=record.scheduled_for,
                sent_at=record.sent_at,
                created_at=record.created_at,
            )
        )
        await self._session.flush()

    async def save(self, record: OutboundMessageRecord) -> None:
        model = await self._session.get(OutboundMessageRecordModel, record.id)
        if model is None:
            raise ValueError(f"outbound_message_record {record.id} not found for save()")
        model.event_types_batched = json.dumps([t.value for t in record.event_types_batched])
        model.status = record.status.value
        model.attempt_count = record.attempt_count
        model.scheduled_for = record.scheduled_for
        model.sent_at = record.sent_at
        await self._session.flush()
