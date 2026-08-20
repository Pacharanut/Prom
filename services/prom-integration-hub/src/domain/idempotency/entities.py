"""Generic HTTP idempotency cache for Hub's own write endpoints — distinct from (and
complementary to) Prom's own external_id/external_source dedup on work-item creation. See
src/application/work_items/use_cases.py:CreateWorkItem for why both layers exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class IdempotencyRecord:
    id: UUID
    customer_id: UUID
    route: str
    idempotency_key: str
    request_hash: str
    response_status: int
    response_body: str
    created_at: datetime
    expires_at: datetime
