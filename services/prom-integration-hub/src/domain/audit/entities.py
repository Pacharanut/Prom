"""AuditLogEntry — append-only record of every security/business-relevant action. Never store
message content or secret values here (see Gate 0/1 §12 security requirements)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from src.domain.integrations.entities import ChannelType


class ResultStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass
class AuditLogEntry:
    id: UUID
    customer_id: UUID | None
    prom_instance_id: UUID | None
    installation_id: UUID | None
    channel_type: ChannelType | None
    channel_user_id: str | None
    prom_user_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    correlation_id: str
    result_status: ResultStatus
    error_code: str | None
    occurred_at: datetime
