"""Work-item timeline (Phase 2E, AI MVP item 10): Plane's own history for one work item, with
each change labelled by where it came from — LINE, Discord, another Hub caller, or Plane itself.

Plane records every change under the user who made it, including changes the Hub made with that
user's delegated token, so Plane alone can't tell "from LINE" apart from "in the web app". The
Hub's audit log can: since Phase 2E every channel write records its origin (AuditOrigin). An
activity is attributed to a channel when a successful Hub write by the *same user*, of a
compatible kind, happened within _MATCH_WINDOW of it. Anything else is "plane". This is a
heuristic (documented in the README): a user editing the same field in the web app within the
same two minutes as a LINE edit could be mislabelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from src.application.work_items.use_cases import _WorkItemUseCaseBase
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.work_items.entities import WorkItemActivity

TimelineSource = Literal["line", "discord", "hub", "plane"]

_MATCH_WINDOW = timedelta(minutes=2)
_ACTION_KINDS: dict[str, frozenset[str]] = {
    "work_item.created": frozenset({"created"}),
    "work_item.create_replayed": frozenset({"created"}),
    "work_item.updated": frozenset({"state"}),
    "work_item.completed": frozenset({"state"}),
    "work_item.assigned": frozenset({"assignees"}),
    "work_item.rescheduled": frozenset({"target_date"}),
    "comment.created": frozenset({"comment"}),
}
"""Hub audit action → the Plane activity kinds it produces. A create also sets state/assignees/
date/labels/priority in one go, so those fields are accepted for "created" too (below)."""
_CREATE_SIDE_FIELDS = frozenset({"state", "assignees", "target_date", "labels", "priority"})
"""Fields the Hub sets *at* creation. Not "name"/"description": the Hub never edits those, so a
rename right after a Hub create was made somewhere else (found live 2026-10-26: renames in the
web app within a minute of a Discord create were mislabelled "Hub")."""


@dataclass
class TimelineEntry:
    at: datetime
    source: TimelineSource
    kind: str
    """"created", "comment", or the Plane field name ("state", "assignees", "target_date", …)."""
    actor_id: str | None
    actor_name: str | None
    old_value: str | None
    new_value: str | None


def _kind(activity: WorkItemActivity) -> str:
    if activity.field in (None, "", "issue") and activity.verb == "created":
        return "created"
    return activity.field or activity.verb


def _accepts(action: str, kind: str) -> bool:
    kinds = _ACTION_KINDS.get(action)
    if kinds is None:
        return False
    if kind in kinds:
        return True
    return "created" in kinds and kind in _CREATE_SIDE_FIELDS


def attribute(activity: WorkItemActivity, hub_entries: list[AuditLogEntry]) -> TimelineSource:
    kind = _kind(activity)
    candidates = [
        e
        for e in hub_entries
        if e.prom_user_id is not None
        and e.prom_user_id == activity.actor_id
        and abs(e.occurred_at - activity.created_at) <= _MATCH_WINDOW
        and _accepts(e.action, kind)
    ]
    if not candidates:
        return "plane"
    closest = min(candidates, key=lambda e: abs(e.occurred_at - activity.created_at))
    if closest.channel_type is None:
        return "hub"
    return "line" if closest.channel_type.value == "line" else "discord"


@dataclass
class GetWorkItemTimeline(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        source: TimelineSource | None = None,
        kind: str | None = None,
        limit: int = 20,
        correlation_id: str,
    ) -> list[TimelineEntry]:
        """Newest first. `source`/`kind` narrow the result (searchable history)."""
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        activities = await self.prom_api_client.list_work_item_activities(
            ctx.base_url, ctx.token, workspace_slug, project_id, work_item_id
        )
        members = await self.prom_api_client.list_project_members(
            ctx.base_url, ctx.token, workspace_slug, project_id
        )
        names = {m.user_id: m.display_name for m in members}
        hub_entries = [
            e
            for e in await self.audit_log.list_for_target("work_item", work_item_id, limit=200)
            if e.result_status is ResultStatus.SUCCESS
        ]

        entries: list[TimelineEntry] = []
        for activity in sorted(activities, key=lambda a: a.created_at, reverse=True):
            entry = TimelineEntry(
                at=activity.created_at,
                source=attribute(activity, hub_entries),
                kind=_kind(activity),
                actor_id=activity.actor_id,
                actor_name=names.get(activity.actor_id or ""),
                old_value=activity.old_value,
                new_value=activity.new_value,
            )
            if source is not None and entry.source != source:
                continue
            if kind is not None and entry.kind != kind:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries
