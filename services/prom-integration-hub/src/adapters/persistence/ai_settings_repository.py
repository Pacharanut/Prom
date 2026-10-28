"""Postgres-backed AiSettingsRepository — one row per customer (unique on customer_id)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import AiSettingsModel
from src.application.ai.ports import AiSettingsRepository
from src.domain.ai.entities import AiSettings


def _to_domain(model: AiSettingsModel) -> AiSettings:
    return AiSettings(
        id=model.id,
        customer_id=model.customer_id,
        is_enabled=model.is_enabled,
        base_url=model.base_url,
        encrypted_api_key=model.encrypted_api_key,
        model=model.model,
        timeout_seconds=model.timeout_seconds,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class PostgresAiSettingsRepository(AiSettingsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_customer(self, customer_id: UUID) -> AiSettings | None:
        result = await self._session.execute(
            select(AiSettingsModel).where(AiSettingsModel.customer_id == customer_id)
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def save(self, settings: AiSettings) -> None:
        result = await self._session.execute(
            select(AiSettingsModel).where(AiSettingsModel.customer_id == settings.customer_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            self._session.add(
                AiSettingsModel(
                    id=settings.id,
                    customer_id=settings.customer_id,
                    is_enabled=settings.is_enabled,
                    base_url=settings.base_url,
                    encrypted_api_key=settings.encrypted_api_key,
                    model=settings.model,
                    timeout_seconds=settings.timeout_seconds,
                    created_at=settings.created_at,
                    updated_at=settings.updated_at,
                )
            )
        else:
            model.is_enabled = settings.is_enabled
            model.base_url = settings.base_url
            model.encrypted_api_key = settings.encrypted_api_key
            model.model = settings.model
            model.timeout_seconds = settings.timeout_seconds
            model.updated_at = settings.updated_at
        await self._session.flush()
