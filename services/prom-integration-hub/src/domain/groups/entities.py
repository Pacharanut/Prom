"""GroupBinding (Phase 2D, AI MVP item 9 "basic workflow automation"): a LINE group/room or a
Discord text channel that receives work-item events for one workspace (optionally one project).
Workflows are predefined, not user-built: each binding just switches the three group events on
or off. Personal reminders (assigned → assignee, overdue → assignee) already exist per user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from src.domain.integrations.entities import ChannelType


class GroupEventType(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    COMPLETED = "completed"


DEFAULT_GROUP_EVENTS: frozenset[GroupEventType] = frozenset({GroupEventType.COMPLETED})
"""Spec example "task complete → notify group"; the noisier two are opt-in."""


@dataclass
class GroupBinding:
    id: UUID
    customer_id: UUID
    channel_installation_id: UUID
    channel_type: ChannelType
    group_id: str
    """LINE groupId/roomId, or Discord channel id."""
    workspace_connection_id: UUID
    project_id: str | None
    """None = every project in the workspace."""
    project_name: str | None
    bound_by_prom_user_id: str
    events: set[GroupEventType] = field(default_factory=lambda: set(DEFAULT_GROUP_EVENTS))
    created_at: datetime | None = None
    updated_at: datetime | None = None
