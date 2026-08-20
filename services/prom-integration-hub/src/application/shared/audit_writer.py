"""AuditWriter — bundles an AuditLogRepository + Clock so every use case (account_linking,
work_items, ...) writes audit entries the same way instead of repeating the same argument list at
every call site. Extracted from account_linking/use_cases.py in Gate 4 once a second module
(work_items) needed the identical helper — not a port itself, just shared plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from src.application.account_linking.ports import AuditLogRepository, Clock
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.integrations.entities import ChannelType


@dataclass
class AuditWriter:
    audit_log: AuditLogRepository
    clock: Clock

    async def write(
        self,
        *,
        customer_id: UUID | None,
        prom_instance_id: UUID | None,
        installation_id: UUID | None,
        channel_type: ChannelType | None,
        channel_user_id: str | None,
        prom_user_id: str | None,
        action: str,
        target_type: str | None,
        target_id: str | None,
        correlation_id: str,
        result_status: ResultStatus,
        error_code: str | None,
    ) -> None:
        await self.audit_log.add(
            AuditLogEntry(
                id=uuid4(),
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=installation_id,
                channel_type=channel_type,
                channel_user_id=channel_user_id,
                prom_user_id=prom_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                correlation_id=correlation_id,
                result_status=result_status,
                error_code=error_code,
                occurred_at=self.clock.now(),
            )
        )
