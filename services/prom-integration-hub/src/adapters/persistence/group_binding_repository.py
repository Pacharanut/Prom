"""Postgres-backed GroupBindingRepository (Phase 2D)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import GroupBindingModel
from src.application.groups.ports import GroupBindingRepository
from src.domain.groups.entities import GroupBinding, GroupEventType
from src.domain.integrations.entities import ChannelType


def _to_domain(model: GroupBindingModel) -> GroupBinding:
    return GroupBinding(
        id=model.id,
        customer_id=model.customer_id,
        channel_installation_id=model.channel_installation_id,
        channel_type=ChannelType(model.channel_type),
        group_id=model.group_id,
        workspace_connection_id=model.workspace_connection_id,
        project_id=model.project_id,
        project_name=model.project_name,
        bound_by_prom_user_id=model.bound_by_prom_user_id,
        events={GroupEventType(v) for v in model.events.split(",") if v},
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _events(binding: GroupBinding) -> str:
    return ",".join(sorted(e.value for e in binding.events))


class PostgresGroupBindingRepository(GroupBindingRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_model(
        self, channel_installation_id: UUID, group_id: str
    ) -> GroupBindingModel | None:
        stmt = select(GroupBindingModel).where(
            GroupBindingModel.channel_installation_id == channel_installation_id,
            GroupBindingModel.group_id == group_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_group(
        self, channel_installation_id: UUID, group_id: str
    ) -> GroupBinding | None:
        model = await self._get_model(channel_installation_id, group_id)
        return _to_domain(model) if model is not None else None

    async def list_for_workspace(self, workspace_connection_id: UUID) -> list[GroupBinding]:
        stmt = select(GroupBindingModel).where(
            GroupBindingModel.workspace_connection_id == workspace_connection_id
        )
        return [_to_domain(m) for m in (await self._session.execute(stmt)).scalars().all()]

    async def save(self, binding: GroupBinding) -> None:
        assert binding.created_at is not None and binding.updated_at is not None
        model = await self._get_model(binding.channel_installation_id, binding.group_id)
        if model is None:
            self._session.add(
                GroupBindingModel(
                    id=binding.id,
                    customer_id=binding.customer_id,
                    channel_installation_id=binding.channel_installation_id,
                    channel_type=binding.channel_type.value,
                    group_id=binding.group_id,
                    workspace_connection_id=binding.workspace_connection_id,
                    project_id=binding.project_id,
                    project_name=binding.project_name,
                    bound_by_prom_user_id=binding.bound_by_prom_user_id,
                    events=_events(binding),
                    created_at=binding.created_at,
                    updated_at=binding.updated_at,
                )
            )
        else:
            model.workspace_connection_id = binding.workspace_connection_id
            model.project_id = binding.project_id
            model.project_name = binding.project_name
            model.bound_by_prom_user_id = binding.bound_by_prom_user_id
            model.events = _events(binding)
            model.updated_at = binding.updated_at
        await self._session.flush()

    async def delete(self, binding_id: UUID) -> None:
        await self._session.execute(
            delete(GroupBindingModel).where(GroupBindingModel.id == binding_id)
        )
        await self._session.flush()
