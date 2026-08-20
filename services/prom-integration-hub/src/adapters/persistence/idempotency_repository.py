"""Postgres-backed IdempotencyRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import IdempotencyRecordModel
from src.application.work_items.ports import IdempotencyRepository
from src.domain.idempotency.entities import IdempotencyRecord
from src.shared.errors import IdempotencyKeyConflictError


def _to_domain(model: IdempotencyRecordModel) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=model.id,
        customer_id=model.customer_id,
        route=model.route,
        idempotency_key=model.idempotency_key,
        request_hash=model.request_hash,
        response_status=model.response_status,
        response_body=model.response_body,
        created_at=model.created_at,
        expires_at=model.expires_at,
    )


class PostgresIdempotencyRepository(IdempotencyRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, customer_id: UUID, route: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        stmt = select(IdempotencyRecordModel).where(
            IdempotencyRecordModel.customer_id == customer_id,
            IdempotencyRecordModel.route == route,
            IdempotencyRecordModel.idempotency_key == idempotency_key,
            IdempotencyRecordModel.expires_at > datetime.now(UTC),
        )
        model = (await self._session.execute(stmt)).scalars().first()
        return _to_domain(model) if model is not None else None

    async def add(self, record: IdempotencyRecord) -> None:
        """SAVEPOINT-wrapped like PostgresLinkedIdentityRepository.add — a concurrent duplicate
        insert under the same (customer_id, route, idempotency_key) is a real, reachable race
        (two retries of the same client request arriving close together) and must not poison the
        caller's transaction, since the idempotency check-then-act sequence in apps/api still has
        other work to do (persisting the actual use-case side effects) around this call."""
        try:
            async with self._session.begin_nested():
                self._session.add(
                    IdempotencyRecordModel(
                        id=record.id,
                        customer_id=record.customer_id,
                        route=record.route,
                        idempotency_key=record.idempotency_key,
                        request_hash=record.request_hash,
                        response_status=record.response_status,
                        response_body=record.response_body,
                        created_at=record.created_at,
                        expires_at=record.expires_at,
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            raise IdempotencyKeyConflictError(
                "an idempotency record for this (customer_id, route, idempotency_key) already "
                "exists"
            ) from exc
