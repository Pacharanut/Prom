"""Daily summary (Phase 2B, AI MVP item 4): one personal morning message — overdue, due today,
due tomorrow, recently assigned, blocked — instead of one push per work item. Returns a
structured DailySummary; the Thai text is composed by src/channels/notification_copy.py and
injected where the message is sent, so this module stays copy-free.

Also home to `resolve_identity_workspace`, shared with CheckDueDateNotifications: an identity's
workspace is its own default, else its installation's default, else the installation's only
linked workspace — the same order the LINE/Discord handlers already use for interactive turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from src.application.account_linking.ports import (
    ChannelInstallationRepository,
    Clock,
    WorkspaceConnectionRepository,
)
from src.application.notifications.ports import (
    NotificationPreferenceRepository,
    OutboundMessageRepository,
)
from src.application.work_items.use_cases import (
    FindBlockedWorkItems,
    ListMyWorkItems,
    ListProjects,
)
from src.domain.identities.entities import LinkedIdentity
from src.domain.notifications.entities import NotificationEventType
from src.domain.work_items.entities import ProjectRef, WorkItem
from src.domain.workspaces.entities import WorkspaceConnection

_TIMEZONE = ZoneInfo("Asia/Bangkok")
_RECENT_WINDOW = timedelta(hours=24)
_SECTION_CAP = 10


@dataclass
class SummaryItem:
    project_id: str
    work_item_id: str
    label: str
    """Display identifier, e.g. "PROM-12"."""
    name: str
    due_date: date | None


@dataclass
class DailySummary:
    day: date
    overdue: list[SummaryItem] = field(default_factory=list)
    due_today: list[SummaryItem] = field(default_factory=list)
    due_soon: list[SummaryItem] = field(default_factory=list)
    recently_assigned: list[SummaryItem] = field(default_factory=list)
    blocked: list[SummaryItem] = field(default_factory=list)
    open_count: int = 0
    """All open work items assigned to the user (after the ListMyWorkItems caps)."""
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not (
            self.overdue
            or self.due_today
            or self.due_soon
            or self.recently_assigned
            or self.blocked
        )


async def resolve_identity_workspace(
    identity: LinkedIdentity,
    installations: ChannelInstallationRepository,
    workspace_connections: WorkspaceConnectionRepository,
) -> WorkspaceConnection | None:
    if identity.default_workspace_connection_id is not None:
        workspace = await workspace_connections.get(identity.default_workspace_connection_id)
        if workspace is not None:
            return workspace
    installation = await installations.get(identity.channel_installation_id)
    if installation is not None and installation.default_workspace_connection_id is not None:
        workspace = await workspace_connections.get(installation.default_workspace_connection_id)
        if workspace is not None:
            return workspace
    links = await installations.list_workspaces(identity.channel_installation_id)
    if len(links) == 1:
        return await workspace_connections.get(links[0].workspace_connection_id)
    return None


def _items(work_items: list[WorkItem], projects: dict[str, ProjectRef]) -> list[SummaryItem]:
    result: list[SummaryItem] = []
    for item in work_items:
        project = projects.get(item.project_id)
        label = f"{project.identifier}-{item.sequence_id}" if project else str(item.sequence_id)
        result.append(
            SummaryItem(
                project_id=item.project_id,
                work_item_id=item.id,
                label=label,
                name=item.name,
                due_date=item.target_date,
            )
        )
    return result[:_SECTION_CAP]


def _by_due_date(item: WorkItem) -> tuple[bool, date]:
    return (item.target_date is None, item.target_date or date.max)


@dataclass
class BuildDailySummary:
    list_projects: ListProjects
    list_my_work_items: ListMyWorkItems
    find_blocked_work_items: FindBlockedWorkItems
    outbound_messages: OutboundMessageRepository
    notification_preferences: NotificationPreferenceRepository
    clock: Clock

    async def execute(
        self, *, identity: LinkedIdentity, workspace_slug: str, correlation_id: str
    ) -> DailySummary:
        """Sections follow the user's per-type switches (overdue / due_today / due_soon /
        assigned); "blocked" has no switch of its own and follows the summary switch."""
        now = self.clock.now()
        today = now.astimezone(_TIMEZONE).date()
        disabled = await self.notification_preferences.list_disabled(identity.id)

        projects = await self.list_projects.execute(
            prom_instance_id=identity.prom_instance_id,
            prom_user_id=identity.prom_user_id,
            workspace_slug=workspace_slug,
            correlation_id=correlation_id,
        )
        mine = await self.list_my_work_items.execute(
            prom_instance_id=identity.prom_instance_id,
            prom_user_id=identity.prom_user_id,
            workspace_slug=workspace_slug,
            project_ids=[p.id for p in projects],
            view="assigned",
            correlation_id=correlation_id,
        )
        open_items = sorted((i for i in mine.items if i.completed_at is None), key=_by_due_date)
        by_id = {p.id: p for p in projects}
        summary = DailySummary(day=today, open_count=len(open_items), truncated=mine.truncated)

        if NotificationEventType.OVERDUE not in disabled:
            summary.overdue = _items(
                [i for i in open_items if i.target_date and i.target_date < today], by_id
            )
        if NotificationEventType.DUE_TODAY not in disabled:
            summary.due_today = _items([i for i in open_items if i.target_date == today], by_id)
        if NotificationEventType.DUE_SOON not in disabled:
            tomorrow = today + timedelta(days=1)
            summary.due_soon = _items([i for i in open_items if i.target_date == tomorrow], by_id)
        if NotificationEventType.ASSIGNED not in disabled:
            records = await self.outbound_messages.list_created_since(
                identity.id, now - _RECENT_WINDOW
            )
            recent_ids = {
                r.work_item_id
                for r in records
                if NotificationEventType.ASSIGNED in r.event_types_batched
            }
            summary.recently_assigned = _items([i for i in open_items if i.id in recent_ids], by_id)

        blocked = await self.find_blocked_work_items.execute(
            prom_instance_id=identity.prom_instance_id,
            prom_user_id=identity.prom_user_id,
            workspace_slug=workspace_slug,
            work_items=open_items,
            correlation_id=correlation_id,
        )
        summary.blocked = _items(blocked, by_id)
        return summary
