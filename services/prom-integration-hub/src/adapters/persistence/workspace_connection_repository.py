"""Postgres-backed WorkspaceConnectionRepository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import WorkspaceConnectionModel
from src.application.account_linking.ports import WorkspaceConnectionRepository
from src.domain.workspaces.entities import WorkspaceConnection


def _to_domain(model: WorkspaceConnectionModel) -> WorkspaceConnection:
    return WorkspaceConnection(
        id=model.id,
        customer_id=model.customer_id,
        prom_instance_id=model.prom_instance_id,
        prom_workspace_id=model.prom_workspace_id,
        prom_workspace_slug=model.prom_workspace_slug,
        name=model.name,
        created_at=model.created_at,
        updated_at=model.updated_at,
        webhook_secret_encrypted=model.webhook_secret_encrypted,
    )


class PostgresWorkspaceConnectionRepository(WorkspaceConnectionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_connection_id: UUID) -> WorkspaceConnection | None:
        model = await self._session.get(WorkspaceConnectionModel, workspace_connection_id)
        return _to_domain(model) if model is not None else None

    async def add(
        self, workspace_connection: WorkspaceConnection
    ) -> tuple[WorkspaceConnection, bool]:
        """SAVEPOINT-wrapped like every other unique-constraint write in this codebase (see
        `PostgresLinkedIdentityRepository.add`/`PostgresWebhookEventRepository.add`): a
        uniqueness violation on (prom_instance_id, prom_workspace_id) must not poison the outer
        transaction. Unlike those two, a conflict here is not an error to surface -- it's a
        get-or-create hit, so the recovery path updates the existing row's `name`/
        `prom_workspace_slug` (Plane's workspace is the source of truth) and returns it.
        """
        try:
            async with self._session.begin_nested():
                self._session.add(
                    WorkspaceConnectionModel(
                        id=workspace_connection.id,
                        customer_id=workspace_connection.customer_id,
                        prom_instance_id=workspace_connection.prom_instance_id,
                        prom_workspace_id=workspace_connection.prom_workspace_id,
                        prom_workspace_slug=workspace_connection.prom_workspace_slug,
                        name=workspace_connection.name,
                        created_at=workspace_connection.created_at,
                        updated_at=workspace_connection.updated_at,
                        webhook_secret_encrypted=workspace_connection.webhook_secret_encrypted,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_by_external_id(
                workspace_connection.prom_instance_id, workspace_connection.prom_workspace_id
            )
            if existing is None:  # pragma: no cover - only reachable via a concurrent delete
                raise
            model = await self._session.get(WorkspaceConnectionModel, existing.id)
            assert model is not None
            model.name = workspace_connection.name
            model.prom_workspace_slug = workspace_connection.prom_workspace_slug
            model.updated_at = workspace_connection.updated_at
            await self._session.flush()
            return _to_domain(model), False
        return workspace_connection, True

    async def get_by_external_id(
        self, prom_instance_id: UUID, prom_workspace_id: str
    ) -> WorkspaceConnection | None:
        stmt = select(WorkspaceConnectionModel).where(
            WorkspaceConnectionModel.prom_instance_id == prom_instance_id,
            WorkspaceConnectionModel.prom_workspace_id == prom_workspace_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def save(self, workspace_connection: WorkspaceConnection) -> None:
        model = await self._session.get(WorkspaceConnectionModel, workspace_connection.id)
        if model is None:
            raise ValueError(f"workspace_connection {workspace_connection.id} not found for save()")
        model.webhook_secret_encrypted = workspace_connection.webhook_secret_encrypted
        await self._session.flush()
