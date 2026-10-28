"""Postgres-backed AuditLogRepository. Append-only for writes — `add()` never updates/deletes;
`list_for_customer()` is the one read method (Gate 8)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import AuditLogEntryModel
from src.application.account_linking.ports import AuditLogFilters, AuditLogRepository
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.integrations.entities import ChannelType


def _to_domain(model: AuditLogEntryModel) -> AuditLogEntry:
    return AuditLogEntry(
        id=model.id,
        customer_id=model.customer_id,
        prom_instance_id=model.prom_instance_id,
        installation_id=model.installation_id,
        channel_type=ChannelType(model.channel_type) if model.channel_type else None,
        channel_user_id=model.channel_user_id,
        prom_user_id=model.prom_user_id,
        action=model.action,
        target_type=model.target_type,
        target_id=model.target_id,
        correlation_id=model.correlation_id,
        result_status=ResultStatus(model.result_status),
        error_code=model.error_code,
        occurred_at=model.occurred_at,
    )


class PostgresAuditLogRepository(AuditLogRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: AuditLogEntry) -> None:
        self._session.add(
            AuditLogEntryModel(
                id=entry.id,
                customer_id=entry.customer_id,
                prom_instance_id=entry.prom_instance_id,
                installation_id=entry.installation_id,
                channel_type=entry.channel_type.value if entry.channel_type else None,
                channel_user_id=entry.channel_user_id,
                prom_user_id=entry.prom_user_id,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                correlation_id=entry.correlation_id,
                result_status=entry.result_status.value,
                error_code=entry.error_code,
                occurred_at=entry.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_customer(
        self,
        customer_id: UUID,
        *,
        installation_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        filters: AuditLogFilters | None = None,
    ) -> list[AuditLogEntry]:
        stmt = select(AuditLogEntryModel).where(AuditLogEntryModel.customer_id == customer_id)
        if installation_id is not None:
            stmt = stmt.where(AuditLogEntryModel.installation_id == installation_id)
        if filters is not None:
            if filters.channel_type is not None:
                stmt = stmt.where(AuditLogEntryModel.channel_type == filters.channel_type.value)
            if filters.action_contains:
                stmt = stmt.where(AuditLogEntryModel.action.contains(filters.action_contains))
            if filters.target_id:
                stmt = stmt.where(AuditLogEntryModel.target_id == filters.target_id)
            if filters.result_status is not None:
                stmt = stmt.where(AuditLogEntryModel.result_status == filters.result_status.value)
            if filters.occurred_after is not None:
                stmt = stmt.where(AuditLogEntryModel.occurred_at >= filters.occurred_after)
            if filters.occurred_before is not None:
                stmt = stmt.where(AuditLogEntryModel.occurred_at < filters.occurred_before)
        stmt = stmt.order_by(AuditLogEntryModel.occurred_at.desc()).limit(limit).offset(offset)
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(model) for model in models]

    async def list_for_target(
        self, target_type: str, target_id: str, *, limit: int = 100
    ) -> list[AuditLogEntry]:
        stmt = (
            select(AuditLogEntryModel)
            .where(
                AuditLogEntryModel.target_type == target_type,
                AuditLogEntryModel.target_id == target_id,
            )
            .order_by(AuditLogEntryModel.occurred_at.desc())
            .limit(limit)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(model) for model in models]
