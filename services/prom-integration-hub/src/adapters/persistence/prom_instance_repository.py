"""Postgres-backed PromInstanceRepository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import PromInstanceModel
from src.application.account_linking.ports import PromInstanceRepository
from src.domain.instances.entities import InstanceStatus, PromInstance


def _to_domain(model: PromInstanceModel) -> PromInstance:
    return PromInstance(
        id=model.id,
        customer_id=model.customer_id,
        name=model.name,
        base_url=model.base_url,
        status=InstanceStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class PostgresPromInstanceRepository(PromInstanceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, instance_id: UUID) -> PromInstance | None:
        model = await self._session.get(PromInstanceModel, instance_id)
        return _to_domain(model) if model is not None else None

    async def add(self, instance: PromInstance) -> None:
        self._session.add(
            PromInstanceModel(
                id=instance.id,
                customer_id=instance.customer_id,
                name=instance.name,
                base_url=instance.base_url,
                status=instance.status.value,
                created_at=instance.created_at,
                updated_at=instance.updated_at,
            )
        )
        await self._session.flush()
