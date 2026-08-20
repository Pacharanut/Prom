"""Postgres-backed WebhookEventRepository — durable dedup + audit journal for inbound Prom
webhook deliveries (see src/application/webhooks/ports.py's module docstring for why this is a
Postgres table rather than the Redis-backed WebhookDedupStore LINE/Discord use).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import WebhookEventRecordModel
from src.application.webhooks.ports import WebhookEventRepository
from src.domain.webhooks.entities import WebhookEventRecord, WebhookEventStatus


def _to_domain(model: WebhookEventRecordModel) -> WebhookEventRecord:
    return WebhookEventRecord(
        id=model.id,
        workspace_connection_id=model.workspace_connection_id,
        delivery_id=model.delivery_id,
        event=model.event,
        action=model.action,
        received_at=model.received_at,
        processed_at=model.processed_at,
        status=WebhookEventStatus(model.status),
    )


class PostgresWebhookEventRepository(WebhookEventRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_delivery_id(self, delivery_id: str) -> WebhookEventRecord | None:
        stmt = select(WebhookEventRecordModel).where(
            WebhookEventRecordModel.delivery_id == delivery_id
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get(self, record_id: UUID) -> WebhookEventRecord | None:
        model = await self._session.get(WebhookEventRecordModel, record_id)
        return _to_domain(model) if model is not None else None

    async def add(self, record: WebhookEventRecord) -> WebhookEventRecord | None:
        """SAVEPOINT-wrapped like every other unique-constraint write in this codebase, but
        unlike PostgresLinkedIdentityRepository.add, a duplicate `delivery_id` here is NOT a
        caller error to surface as a typed DomainError — it's the expected shape of "Plane
        retried a delivery we already recorded," i.e. exactly what this table exists to detect.
        Swallow it and return None; the router's pre-insert `get_by_delivery_id` check already
        handles the common case, this is only the narrow concurrent-race window between that
        check and this insert.
        """
        try:
            async with self._session.begin_nested():
                self._session.add(
                    WebhookEventRecordModel(
                        id=record.id,
                        workspace_connection_id=record.workspace_connection_id,
                        delivery_id=record.delivery_id,
                        event=record.event,
                        action=record.action,
                        received_at=record.received_at,
                        processed_at=record.processed_at,
                        status=record.status.value,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            return None
        return record

    async def save(self, record: WebhookEventRecord) -> None:
        model = await self._session.get(WebhookEventRecordModel, record.id)
        if model is None:
            raise ValueError(f"webhook_event_record {record.id} not found for save()")
        model.processed_at = record.processed_at
        model.status = record.status.value
        await self._session.flush()
