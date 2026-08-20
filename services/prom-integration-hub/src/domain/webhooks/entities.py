"""WebhookEventRecord — idempotency + audit journal for inbound Prom (Plane) webhook deliveries.
New domain subfolder, same precedent as `src/domain/idempotency/` (Gate 4): not part of the
master prompt's original static folder list, added because the concept didn't fit cleanly under
any of the existing ones (it's neither a `notifications` concept — that's what it *triggers* —
nor `audit`, which is for actions Hub itself performed, not raw inbound deliveries).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class WebhookEventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class WebhookEventRecord:
    id: UUID
    workspace_connection_id: UUID
    delivery_id: str
    """Plane's `X-Plane-Delivery` header value (a UUID Plane mints per delivery attempt) — the
    idempotency key. Not derived by Hub; used verbatim, per Gate 7 spec."""
    event: str
    action: str
    received_at: datetime
    processed_at: datetime | None
    status: WebhookEventStatus
