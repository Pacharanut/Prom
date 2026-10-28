"""Notification domain entities — Gate 7. `NotificationPreference` lets a user mute one event
type per §8.6 ("User เปิด/ปิด Notification แต่ละประเภทได้"); `OutboundMessageRecord` is the
debounce buffer + retry/dead-letter ledger for the actual LINE/Discord send that
`SendPrivateNotification` (src/application/notifications/use_cases.py, built in Gate 5) performs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class NotificationEventType(StrEnum):
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    STATE_CHANGED = "state_changed"
    COMMENT = "comment"
    COMPLETED = "completed"
    DUE_SOON = "due_soon"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
    DAILY_SUMMARY = "daily_summary"
    """Phase 2B: one morning message instead of one message per due/overdue work item. While
    enabled (the default), the per-item due_soon/due_today/overdue pushes are folded into it."""


@dataclass
class NotificationPreference:
    id: UUID
    customer_id: UUID
    linked_identity_id: UUID
    event_type: NotificationEventType
    enabled: bool
    """Absence of a row for (linked_identity_id, event_type) means "enabled" (default-on, opt-out
    model per §8.6) — only an explicit row with enabled=False suppresses that type. See
    NotificationPreferenceRepository.is_enabled."""


class OutboundMessageStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DEAD_LETTER = "dead_letter"


@dataclass
class OutboundMessageRecord:
    id: UUID
    linked_identity_id: UUID
    work_item_id: str
    """Plane's external work-item id (str) — Hub never persists work items themselves, this is
    just a grouping key for debounce, same string used everywhere else in the codebase
    (WorkItem.id)."""
    event_types_batched: list[NotificationEventType]
    status: OutboundMessageStatus
    attempt_count: int
    scheduled_for: datetime
    sent_at: datetime | None
    created_at: datetime
