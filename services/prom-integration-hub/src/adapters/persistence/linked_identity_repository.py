"""Postgres-backed LinkedIdentityRepository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import LinkedIdentityModel
from src.application.account_linking.ports import LinkedIdentityRepository
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.integrations.entities import ChannelType
from src.shared.errors import IdentityAlreadyLinkedError


def _to_domain(model: LinkedIdentityModel) -> LinkedIdentity:
    return LinkedIdentity(
        id=model.id,
        customer_id=model.customer_id,
        channel_installation_id=model.channel_installation_id,
        channel_type=ChannelType(model.channel_type),
        channel_user_id=model.channel_user_id,
        prom_instance_id=model.prom_instance_id,
        prom_user_id=model.prom_user_id,
        default_workspace_connection_id=model.default_workspace_connection_id,
        status=IdentityStatus(model.status),
        linked_at=model.linked_at,
        unlinked_at=model.unlinked_at,
    )


class PostgresLinkedIdentityRepository(LinkedIdentityRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, linked_identity_id: UUID) -> LinkedIdentity | None:
        model = await self._session.get(LinkedIdentityModel, linked_identity_id)
        return _to_domain(model) if model is not None else None

    async def add(self, identity: LinkedIdentity) -> None:
        """Persist a new LinkedIdentity.

        Wrapped in a SAVEPOINT (`begin_nested`) rather than flushing directly against the
        session's main transaction: a uniqueness violation here (an active identity already
        exists for this channel_user_id or prom_user_id on the installation — see the partial
        unique indexes on `linked_identities`) must not poison the whole request's transaction,
        because callers (e.g. `ConsumeLinkingCode`) still need to write a failure audit-log entry
        in the same session/transaction afterward. `begin_nested()` rolls back only the savepoint
        on error, leaving the outer transaction healthy.
        """
        try:
            async with self._session.begin_nested():
                self._session.add(
                    LinkedIdentityModel(
                        id=identity.id,
                        customer_id=identity.customer_id,
                        channel_installation_id=identity.channel_installation_id,
                        channel_type=identity.channel_type.value,
                        channel_user_id=identity.channel_user_id,
                        prom_instance_id=identity.prom_instance_id,
                        prom_user_id=identity.prom_user_id,
                        default_workspace_connection_id=identity.default_workspace_connection_id,
                        status=identity.status.value,
                        linked_at=identity.linked_at,
                        unlinked_at=identity.unlinked_at,
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            raise IdentityAlreadyLinkedError(
                "an active linked identity already exists for this channel user or Prom user "
                "on this installation"
            ) from exc

    async def save(self, identity: LinkedIdentity) -> None:
        model = await self._session.get(LinkedIdentityModel, identity.id)
        if model is None:
            raise ValueError(f"linked_identity {identity.id} not found for save()")
        model.status = identity.status.value
        model.unlinked_at = identity.unlinked_at
        model.default_workspace_connection_id = identity.default_workspace_connection_id
        await self._session.flush()

    async def get_active_by_channel_user(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> LinkedIdentity | None:
        stmt = select(LinkedIdentityModel).where(
            LinkedIdentityModel.channel_installation_id == channel_installation_id,
            LinkedIdentityModel.channel_user_id == channel_user_id,
            LinkedIdentityModel.status == IdentityStatus.ACTIVE.value,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def list_active_by_prom_user(
        self, prom_instance_id: UUID, prom_user_id: str
    ) -> list[LinkedIdentity]:
        stmt = select(LinkedIdentityModel).where(
            LinkedIdentityModel.prom_instance_id == prom_instance_id,
            LinkedIdentityModel.prom_user_id == prom_user_id,
            LinkedIdentityModel.status == IdentityStatus.ACTIVE.value,
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(m) for m in models]

    async def list_all_active_with_default_workspace(self) -> list[LinkedIdentity]:
        stmt = select(LinkedIdentityModel).where(
            LinkedIdentityModel.status == IdentityStatus.ACTIVE.value,
            LinkedIdentityModel.default_workspace_connection_id.is_not(None),
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(m) for m in models]

    async def exists_for_installation(self, channel_installation_id: UUID) -> bool:
        stmt = select(LinkedIdentityModel.id).where(
            LinkedIdentityModel.channel_installation_id == channel_installation_id
        )
        return (await self._session.execute(stmt)).first() is not None
