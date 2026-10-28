"""GroupBindingService (Phase 2D) — what the LINE/Discord handlers call for the in-group commands
"ผูกกลุ่ม" (bind), "ตั้งค่ากลุ่ม" (toggle events) and "ยกเลิกผูกกลุ่ม" (unbind).

Only a user who has linked their own account on this installation can bind or change a group, and
the project offered/accepted is always re-checked against that user's own project list — so a
group can only ever receive titles of work items its binder could already see.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from src.application.account_linking.ports import AuditLogRepository, Clock
from src.application.groups.ports import GroupBindingRepository
from src.application.shared.audit_writer import AuditWriter
from src.application.work_items.use_cases import ListProjects
from src.domain.audit.entities import ResultStatus
from src.domain.groups.entities import DEFAULT_GROUP_EVENTS, GroupBinding, GroupEventType
from src.domain.identities.entities import LinkedIdentity
from src.domain.integrations.entities import ChannelInstallation
from src.domain.work_items.entities import ProjectRef
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import NotFoundError

_PROJECT_PICKER_CAP = 11
"""LINE Quick Reply holds 13 items: "ทุก Project" + up to 11 projects + "ยกเลิก"."""


@dataclass
class GroupBindingService:
    bindings: GroupBindingRepository
    list_projects: ListProjects
    audit_log: AuditLogRepository
    clock: Clock

    async def binding(
        self, installation: ChannelInstallation, group_id: str
    ) -> GroupBinding | None:
        return await self.bindings.get_by_group(installation.id, group_id)

    async def projects(
        self, identity: LinkedIdentity, workspace: WorkspaceConnection, correlation_id: str
    ) -> list[ProjectRef]:
        projects = await self.list_projects.execute(
            prom_instance_id=identity.prom_instance_id,
            prom_user_id=identity.prom_user_id,
            workspace_slug=workspace.prom_workspace_slug,
            correlation_id=correlation_id,
        )
        return projects[:_PROJECT_PICKER_CAP]

    async def bind(
        self,
        *,
        installation: ChannelInstallation,
        group_id: str,
        identity: LinkedIdentity,
        workspace: WorkspaceConnection,
        project_id: str | None,
        correlation_id: str,
    ) -> GroupBinding:
        project_name: str | None = None
        if project_id is not None:
            projects = await self.list_projects.execute(
                prom_instance_id=identity.prom_instance_id,
                prom_user_id=identity.prom_user_id,
                workspace_slug=workspace.prom_workspace_slug,
                correlation_id=correlation_id,
            )
            project = next((p for p in projects if p.id == project_id), None)
            if project is None:
                raise NotFoundError("project not found among the binder's projects")
            project_name = project.name

        now = self.clock.now()
        existing = await self.bindings.get_by_group(installation.id, group_id)
        binding = GroupBinding(
            id=existing.id if existing else uuid4(),
            customer_id=installation.customer_id,
            channel_installation_id=installation.id,
            channel_type=installation.channel_type,
            group_id=group_id,
            workspace_connection_id=workspace.id,
            project_id=project_id,
            project_name=project_name,
            bound_by_prom_user_id=identity.prom_user_id,
            events=set(existing.events) if existing else set(DEFAULT_GROUP_EVENTS),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        await self.bindings.save(binding)
        await self._audit(identity, "group.bound", group_id, correlation_id)
        return binding

    async def unbind(
        self,
        *,
        installation: ChannelInstallation,
        group_id: str,
        identity: LinkedIdentity,
        correlation_id: str,
    ) -> bool:
        existing = await self.bindings.get_by_group(installation.id, group_id)
        if existing is None:
            return False
        await self.bindings.delete(existing.id)
        await self._audit(identity, "group.unbound", group_id, correlation_id)
        return True

    async def toggle(
        self,
        *,
        installation: ChannelInstallation,
        group_id: str,
        identity: LinkedIdentity,
        event_value: str,
        correlation_id: str,
    ) -> GroupBinding | None:
        """Flips one event. Unknown values (stale/forged buttons) change nothing."""
        binding = await self.bindings.get_by_group(installation.id, group_id)
        if binding is None:
            return None
        try:
            event = GroupEventType(event_value)
        except ValueError:
            return binding
        if event in binding.events:
            binding.events.discard(event)
        else:
            binding.events.add(event)
        binding.updated_at = self.clock.now()
        await self.bindings.save(binding)
        await self._audit(identity, "group.settings_updated", group_id, correlation_id)
        return binding

    async def _audit(
        self, identity: LinkedIdentity, action: str, group_id: str, correlation_id: str
    ) -> None:
        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=identity.customer_id,
            prom_instance_id=identity.prom_instance_id,
            installation_id=identity.channel_installation_id,
            channel_type=identity.channel_type,
            channel_user_id=identity.channel_user_id,
            prom_user_id=identity.prom_user_id,
            action=action,
            target_type="group",
            target_id=group_id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
