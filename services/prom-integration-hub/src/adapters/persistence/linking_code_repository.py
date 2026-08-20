"""Postgres-backed LinkingCodeRepository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import LinkingCodeModel
from src.application.account_linking.ports import LinkingCodeRepository
from src.domain.identities.entities import LinkingCode


def _to_domain(model: LinkingCodeModel) -> LinkingCode:
    return LinkingCode(
        id=model.id,
        customer_id=model.customer_id,
        prom_instance_id=model.prom_instance_id,
        prom_user_id=model.prom_user_id,
        channel_installation_id=model.channel_installation_id,
        code=model.code,
        default_workspace_connection_id=model.default_workspace_connection_id,
        expires_at=model.expires_at,
        used_at=model.used_at,
        created_at=model.created_at,
    )


class PostgresLinkingCodeRepository(LinkingCodeRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, linking_code: LinkingCode) -> None:
        self._session.add(
            LinkingCodeModel(
                id=linking_code.id,
                customer_id=linking_code.customer_id,
                prom_instance_id=linking_code.prom_instance_id,
                prom_user_id=linking_code.prom_user_id,
                channel_installation_id=linking_code.channel_installation_id,
                code=linking_code.code,
                default_workspace_connection_id=linking_code.default_workspace_connection_id,
                expires_at=linking_code.expires_at,
                used_at=linking_code.used_at,
                created_at=linking_code.created_at,
            )
        )
        await self._session.flush()

    async def save(self, linking_code: LinkingCode) -> None:
        model = await self._session.get(LinkingCodeModel, linking_code.id)
        if model is None:
            raise ValueError(f"linking_code {linking_code.id} not found for save()")
        model.used_at = linking_code.used_at
        await self._session.flush()

    async def get_by_code(self, channel_installation_id: UUID, code: str) -> LinkingCode | None:
        stmt = select(LinkingCodeModel).where(
            LinkingCodeModel.channel_installation_id == channel_installation_id,
            LinkingCodeModel.code == code,
        )
        model = (await self._session.execute(stmt)).scalars().first()
        return _to_domain(model) if model is not None else None

    async def list_active_codes(self, channel_installation_id: UUID) -> list[str]:
        stmt = select(LinkingCodeModel.code).where(
            LinkingCodeModel.channel_installation_id == channel_installation_id,
            LinkingCodeModel.used_at.is_(None),
            LinkingCodeModel.expires_at > func.now(),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def get_active_for_user(
        self, channel_installation_id: UUID, prom_user_id: str
    ) -> LinkingCode | None:
        stmt = select(LinkingCodeModel).where(
            LinkingCodeModel.channel_installation_id == channel_installation_id,
            LinkingCodeModel.prom_user_id == prom_user_id,
            LinkingCodeModel.used_at.is_(None),
            LinkingCodeModel.expires_at > func.now(),
        )
        model = (await self._session.execute(stmt)).scalars().first()
        return _to_domain(model) if model is not None else None
