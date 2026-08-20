"""Customer — the tenant boundary. Every tenant-owned entity in this service carries a
customer_id that must be checked before any cross-entity operation (see TenantIsolationError).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CustomerStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


@dataclass
class Customer:
    id: UUID
    name: str
    status: CustomerStatus
    created_at: datetime
    updated_at: datetime
