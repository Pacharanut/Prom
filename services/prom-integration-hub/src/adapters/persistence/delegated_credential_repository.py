"""Postgres-backed DelegatedCredentialRepository, keyed by (prom_instance_id, prom_user_id)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import DelegatedCredentialModel
from src.application.account_linking.ports import DelegatedCredentialRepository
from src.domain.identities.entities import DelegatedCredential


def _to_domain(model: DelegatedCredentialModel) -> DelegatedCredential:
    return DelegatedCredential(
        id=model.id,
        customer_id=model.customer_id,
        prom_instance_id=model.prom_instance_id,
        prom_user_id=model.prom_user_id,
        encrypted_token=model.encrypted_token,
        created_at=model.created_at,
        updated_at=model.updated_at,
        rotated_at=model.rotated_at,
    )


class PostgresDelegatedCredentialRepository(DelegatedCredentialRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, prom_instance_id: UUID, prom_user_id: str) -> DelegatedCredential | None:
        stmt = select(DelegatedCredentialModel).where(
            DelegatedCredentialModel.prom_instance_id == prom_instance_id,
            DelegatedCredentialModel.prom_user_id == prom_user_id,
        )
        model = (await self._session.execute(stmt)).scalars().first()
        return _to_domain(model) if model is not None else None

    async def get_any_for_instance(self, prom_instance_id: UUID) -> DelegatedCredential | None:
        stmt = select(DelegatedCredentialModel).where(
            DelegatedCredentialModel.prom_instance_id == prom_instance_id
        )
        model = (await self._session.execute(stmt)).scalars().first()
        return _to_domain(model) if model is not None else None

    async def upsert(self, credential: DelegatedCredential) -> None:
        existing = await self.get(credential.prom_instance_id, credential.prom_user_id)
        if existing is None:
            self._session.add(
                DelegatedCredentialModel(
                    id=credential.id,
                    customer_id=credential.customer_id,
                    prom_instance_id=credential.prom_instance_id,
                    prom_user_id=credential.prom_user_id,
                    encrypted_token=credential.encrypted_token,
                    created_at=credential.created_at,
                    updated_at=credential.updated_at,
                    rotated_at=credential.rotated_at,
                )
            )
        else:
            model = await self._session.get(DelegatedCredentialModel, existing.id)
            assert model is not None
            model.encrypted_token = credential.encrypted_token
            model.updated_at = credential.updated_at
            model.rotated_at = credential.updated_at
        await self._session.flush()
