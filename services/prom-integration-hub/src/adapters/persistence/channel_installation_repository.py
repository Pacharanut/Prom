"""Postgres-backed ChannelInstallationRepository, including installation<->workspace links."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import (
    ChannelInstallationModel,
    InstallationWorkspaceModel,
    LinkingCodeModel,
)
from src.application.account_linking.ports import ChannelInstallationRepository
from src.domain.integrations.entities import (
    ChannelInstallation,
    ChannelType,
    InstallationStatus,
    InstallationWorkspace,
)


def _installation_to_domain(model: ChannelInstallationModel) -> ChannelInstallation:
    return ChannelInstallation(
        id=model.id,
        customer_id=model.customer_id,
        channel_type=ChannelType(model.channel_type),
        display_name=model.display_name,
        status=InstallationStatus(model.status),
        external_account_id=model.external_account_id,
        encrypted_credentials=model.encrypted_credentials,
        default_workspace_connection_id=model.default_workspace_connection_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _link_to_domain(model: InstallationWorkspaceModel) -> InstallationWorkspace:
    return InstallationWorkspace(
        id=model.id,
        channel_installation_id=model.channel_installation_id,
        workspace_connection_id=model.workspace_connection_id,
        created_at=model.created_at,
    )


class PostgresChannelInstallationRepository(ChannelInstallationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, installation_id: UUID) -> ChannelInstallation | None:
        model = await self._session.get(ChannelInstallationModel, installation_id)
        return _installation_to_domain(model) if model is not None else None

    async def add(self, installation: ChannelInstallation) -> None:
        self._session.add(
            ChannelInstallationModel(
                id=installation.id,
                customer_id=installation.customer_id,
                channel_type=installation.channel_type.value,
                display_name=installation.display_name,
                status=installation.status.value,
                external_account_id=installation.external_account_id,
                encrypted_credentials=installation.encrypted_credentials,
                default_workspace_connection_id=installation.default_workspace_connection_id,
                created_at=installation.created_at,
                updated_at=installation.updated_at,
            )
        )
        await self._session.flush()

    async def save(self, installation: ChannelInstallation) -> None:
        model = await self._session.get(ChannelInstallationModel, installation.id)
        if model is None:
            raise ValueError(f"channel_installation {installation.id} not found for save()")
        model.display_name = installation.display_name
        model.status = installation.status.value
        model.default_workspace_connection_id = installation.default_workspace_connection_id
        model.updated_at = installation.updated_at
        await self._session.flush()

    async def add_workspace_link(self, link: InstallationWorkspace) -> None:
        self._session.add(
            InstallationWorkspaceModel(
                id=link.id,
                channel_installation_id=link.channel_installation_id,
                workspace_connection_id=link.workspace_connection_id,
                created_at=link.created_at,
            )
        )
        await self._session.flush()

    async def get_workspace_link(
        self, channel_installation_id: UUID, workspace_connection_id: UUID
    ) -> InstallationWorkspace | None:
        stmt = select(InstallationWorkspaceModel).where(
            InstallationWorkspaceModel.channel_installation_id == channel_installation_id,
            InstallationWorkspaceModel.workspace_connection_id == workspace_connection_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _link_to_domain(model) if model is not None else None

    async def list_workspaces(self, channel_installation_id: UUID) -> list[InstallationWorkspace]:
        stmt = select(InstallationWorkspaceModel).where(
            InstallationWorkspaceModel.channel_installation_id == channel_installation_id
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_link_to_domain(model) for model in models]

    async def list_active_by_channel_type(
        self, channel_type: ChannelType
    ) -> list[ChannelInstallation]:
        stmt = select(ChannelInstallationModel).where(
            ChannelInstallationModel.channel_type == channel_type.value,
            ChannelInstallationModel.status == InstallationStatus.ACTIVE.value,
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_installation_to_domain(model) for model in models]

    async def list_by_customer(self, customer_id: UUID) -> list[ChannelInstallation]:
        stmt = select(ChannelInstallationModel).where(
            ChannelInstallationModel.customer_id == customer_id
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_installation_to_domain(model) for model in models]

    async def remove_workspace_link(
        self, channel_installation_id: UUID, workspace_connection_id: UUID
    ) -> bool:
        # Select-then-delete rather than a bare DELETE + rowcount check: the async driver's
        # Result type doesn't statically expose `.rowcount` (mypy: "Result[Any] has no attribute
        # rowcount"), and this is no less efficient for a single-row-by-unique-pair lookup.
        stmt = select(InstallationWorkspaceModel).where(
            InstallationWorkspaceModel.channel_installation_id == channel_installation_id,
            InstallationWorkspaceModel.workspace_connection_id == workspace_connection_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True

    async def delete(self, installation_id: UUID) -> None:
        # Own workspace-link and linking-code rows first (FK-referenced, no ON DELETE CASCADE) --
        # DeleteChannelInstallation has already confirmed zero LinkedIdentity rows exist, so
        # nothing else references this installation by the time the installation row itself goes.
        await self._session.execute(
            sa_delete(InstallationWorkspaceModel).where(
                InstallationWorkspaceModel.channel_installation_id == installation_id
            )
        )
        await self._session.execute(
            sa_delete(LinkingCodeModel).where(
                LinkingCodeModel.channel_installation_id == installation_id
            )
        )
        await self._session.execute(
            sa_delete(ChannelInstallationModel).where(
                ChannelInstallationModel.id == installation_id
            )
        )
        await self._session.flush()
