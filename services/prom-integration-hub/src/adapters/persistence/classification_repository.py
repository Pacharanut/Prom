"""Postgres-backed ClassificationRepository (Phase 2C)."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import ClassificationRuleModel, ClassificationSettingsModel
from src.application.classification.ports import ClassificationRepository
from src.domain.classification.entities import (
    ClassificationRule,
    ClassificationSettings,
    Priority,
)


class PostgresClassificationRepository(ClassificationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_connection_id: UUID) -> ClassificationSettings:
        row = await self._session.get(ClassificationSettingsModel, workspace_connection_id)
        rules_stmt = (
            select(ClassificationRuleModel)
            .where(ClassificationRuleModel.workspace_connection_id == workspace_connection_id)
            .order_by(ClassificationRuleModel.position)
        )
        rule_rows = (await self._session.execute(rules_stmt)).scalars().all()
        rules = [
            ClassificationRule(
                keyword=r.keyword,
                label_name=r.label_name,
                priority=Priority(r.priority) if r.priority else None,
            )
            for r in rule_rows
        ]
        if row is None:
            return ClassificationSettings(
                workspace_connection_id=workspace_connection_id, rules=rules
            )
        return ClassificationSettings(
            workspace_connection_id=workspace_connection_id,
            is_enabled=row.is_enabled,
            use_ai_fallback=row.use_ai_fallback,
            rules=rules,
            updated_at=row.updated_at,
        )

    async def save(self, settings: ClassificationSettings) -> None:
        assert settings.updated_at is not None, "save() requires updated_at"
        row = await self._session.get(ClassificationSettingsModel, settings.workspace_connection_id)
        if row is None:
            self._session.add(
                ClassificationSettingsModel(
                    workspace_connection_id=settings.workspace_connection_id,
                    is_enabled=settings.is_enabled,
                    use_ai_fallback=settings.use_ai_fallback,
                    updated_at=settings.updated_at,
                )
            )
        else:
            row.is_enabled = settings.is_enabled
            row.use_ai_fallback = settings.use_ai_fallback
            row.updated_at = settings.updated_at
        await self._session.execute(
            delete(ClassificationRuleModel).where(
                ClassificationRuleModel.workspace_connection_id == settings.workspace_connection_id
            )
        )
        for position, rule in enumerate(settings.rules):
            self._session.add(
                ClassificationRuleModel(
                    id=uuid4(),
                    workspace_connection_id=settings.workspace_connection_id,
                    position=position,
                    keyword=rule.keyword,
                    label_name=rule.label_name,
                    priority=rule.priority.value if rule.priority else None,
                )
            )
        await self._session.flush()
