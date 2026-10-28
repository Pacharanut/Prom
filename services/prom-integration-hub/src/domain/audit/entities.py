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


@dataclass(frozen=True)
class AuditOrigin:
    """Phase 2E: which chat a work-item write came from. Passed by the LINE/Discord flows and the
    AI assistant so the audit entry — and the per-work-item timeline built from it — can say
    "from LINE" instead of leaving installation/channel blank."""

    installation_id: UUID
    channel_type: ChannelType
    channel_user_id: str
