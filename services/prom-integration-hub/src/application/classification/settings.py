"""Read/replace a workspace's auto-tagging settings (Phase 2C) — what the Settings → AI
Assistant page's "Auto-tagging" section edits through prom_bridge."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.application.account_linking.ports import (
    AuditLogRepository,
    Clock,
    WorkspaceConnectionRepository,
)
from src.application.classification.ports import ClassificationRepository
from src.application.shared.audit_writer import AuditWriter
from src.domain.audit.entities import ResultStatus
from src.domain.classification.entities import (
    MAX_RULES,
    ClassificationRule,
    ClassificationSettings,
    InvalidClassificationRuleError,
    Priority,
)
from src.shared.errors import NotFoundError, PromRequestInvalidError


@dataclass
class RuleInput:
    keyword: str
    label_name: str | None
    priority: str | None


@dataclass
class GetClassificationSettings:
    workspace_connections: WorkspaceConnectionRepository
    classification: ClassificationRepository

    async def execute(self, *, workspace_connection_id: UUID) -> ClassificationSettings:
        if await self.workspace_connections.get(workspace_connection_id) is None:
            raise NotFoundError(f"workspace connection {workspace_connection_id} not found")
        return await self.classification.get(workspace_connection_id)


@dataclass
class UpdateClassificationSettings:
    workspace_connections: WorkspaceConnectionRepository
    classification: ClassificationRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        workspace_connection_id: UUID,
        is_enabled: bool,
        use_ai_fallback: bool,
        rules: list[RuleInput],
        correlation_id: str,
    ) -> ClassificationSettings:
        workspace = await self.workspace_connections.get(workspace_connection_id)
        if workspace is None:
            raise NotFoundError(f"workspace connection {workspace_connection_id} not found")
        if len(rules) > MAX_RULES:
            raise PromRequestInvalidError(f"at most {MAX_RULES} rules are allowed")
        try:
            parsed = [
                ClassificationRule(
                    keyword=rule.keyword,
                    label_name=rule.label_name,
                    priority=Priority(rule.priority) if rule.priority else None,
                )
                for rule in rules
            ]
        except (InvalidClassificationRuleError, ValueError) as exc:
            raise PromRequestInvalidError(str(exc)) from exc

        settings = ClassificationSettings(
            workspace_connection_id=workspace_connection_id,
            is_enabled=is_enabled,
            use_ai_fallback=use_ai_fallback,
            rules=parsed,
            updated_at=self.clock.now(),
        )
        await self.classification.save(settings)
        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=workspace.customer_id,
            prom_instance_id=workspace.prom_instance_id,
            installation_id=None,
            channel_type=None,
            channel_user_id=None,
            prom_user_id=None,
            action="classification_settings.updated",
            target_type="workspace_connection",
            target_id=str(workspace_connection_id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return settings
