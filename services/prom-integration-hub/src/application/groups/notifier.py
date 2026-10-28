"""GroupNotifier (Phase 2D): turn one verified Plane webhook delivery into messages for every
bound group whose project scope and event switches match. Runs inside the existing
ProcessPromWebhookEvent (already deduplicated by Plane's delivery id), so a retried delivery
never posts twice. Best-effort: one group's delivery failure never affects another group or the
personal notifications.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.application.account_linking.ports import (
    AuditLogRepository,
    ChannelInstallationRepository,
    Clock,
    SecretProvider,
)
from src.application.groups.ports import GroupBindingRepository, GroupMessagePublisher
from src.application.shared.audit_writer import AuditWriter
from src.domain.audit.entities import ResultStatus
from src.domain.groups.entities import GroupBinding, GroupEventType
from src.domain.integrations.entities import InstallationStatus
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import DomainError


@dataclass
class GroupEvent:
    event_type: GroupEventType
    work_item_id: str
    project_id: str | None
    name: str
    sequence_id: int | None
    actor_name: str | None
    assignee_names: list[str] = field(default_factory=list)


def _assignees(data: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in data.get("assignees") or []:
        if isinstance(entry, dict) and entry.get("id"):
            result[str(entry["id"])] = str(
                entry.get("display_name") or entry.get("first_name") or "?"
            )
    return result


def group_events_for(payload: dict[str, Any]) -> list[GroupEvent]:
    """Pure. Same field names determine_notifications relies on (verified against real Plane
    deliveries, see its docstring): action "created"/"updated", activity.field "assignee_ids" /
    "state_id", data.state.group == "completed"."""
    if payload.get("event") != "issue":
        return []
    data = payload.get("data") or {}
    work_item_id = data.get("id")
    if not work_item_id:
        return []
    activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
    actor = activity.get("actor") if isinstance(activity, dict) else None
    actor_name = (
        str(actor.get("display_name") or actor.get("first_name"))
        if isinstance(actor, dict) and (actor.get("display_name") or actor.get("first_name"))
        else None
    )
    sequence = data.get("sequence_id")
    project_id = str(data["project"]) if data.get("project") else None

    def make(event_type: GroupEventType, assignee_names: list[str] | None = None) -> GroupEvent:
        return GroupEvent(
            event_type=event_type,
            work_item_id=str(work_item_id),
            project_id=project_id,
            name=str(data.get("name") or ""),
            sequence_id=sequence if isinstance(sequence, int) else None,
            actor_name=actor_name,
            assignee_names=assignee_names or [],
        )

    assignees = _assignees(data)
    action = payload.get("action")
    if action == "created":
        return [make(GroupEventType.CREATED, list(assignees.values()))]
    if action != "updated" or not isinstance(activity, dict):
        return []
    field_name = activity.get("field")
    if field_name == "assignee_ids":
        added = set(activity.get("new_value") or []) - set(activity.get("old_value") or [])
        names = [assignees.get(str(uid), "?") for uid in added]
        return [make(GroupEventType.ASSIGNED, names)] if names else []
    if field_name == "state_id":
        state = data.get("state") or {}
        if isinstance(state, dict) and state.get("group") == "completed":
            return [make(GroupEventType.COMPLETED)]
    return []


def _matches(binding: GroupBinding, event: GroupEvent) -> bool:
    if event.event_type not in binding.events:
        return False
    return binding.project_id is None or binding.project_id == event.project_id


@dataclass
class GroupNotifier:
    bindings: GroupBindingRepository
    installations: ChannelInstallationRepository
    secrets_provider: SecretProvider
    publisher: GroupMessagePublisher
    compose: Callable[[GroupEvent, GroupBinding], str]
    """Injected from src/channels/group_copy.py — application code stays copy-free."""
    audit_log: AuditLogRepository
    clock: Clock

    async def notify(
        self,
        workspace_connection: WorkspaceConnection,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> int:
        events = group_events_for(payload)
        if not events:
            return 0
        bindings = await self.bindings.list_for_workspace(workspace_connection.id)
        sent = 0
        for binding in bindings:
            for event in events:
                if not _matches(binding, event):
                    continue
                if await self._send(binding, event, correlation_id):
                    sent += 1
        return sent

    async def _send(self, binding: GroupBinding, event: GroupEvent, correlation_id: str) -> bool:
        installation = await self.installations.get(binding.channel_installation_id)
        if installation is None or installation.status is not InstallationStatus.ACTIVE:
            return False
        credentials: dict[str, str] = json.loads(
            self.secrets_provider.decrypt(installation.encrypted_credentials).decode("utf-8")
        )
        error_code: str | None = None
        try:
            await self.publisher.send_group_message(
                binding.channel_type, credentials, binding.group_id, self.compose(event, binding)
            )
        except DomainError as exc:
            error_code = exc.error_code
        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=binding.customer_id,
            prom_instance_id=None,
            installation_id=binding.channel_installation_id,
            channel_type=binding.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="group.notified" if error_code is None else "group.notify_failed",
            target_type="work_item",
            target_id=event.work_item_id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS if error_code is None else ResultStatus.FAILURE,
            error_code=error_code,
        )
        return error_code is None
