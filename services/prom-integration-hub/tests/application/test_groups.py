"""Phase 2D: Plane payload → group events, GroupNotifier fan-out, GroupBindingService."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.application.groups.notifier import GroupNotifier, group_events_for
from src.application.groups.service import GroupBindingService
from src.application.work_items.use_cases import ListProjects
from src.channels.group_copy import compose_group_event
from src.domain.groups.entities import GroupBinding, GroupEventType
from src.domain.identities.entities import DelegatedCredential, IdentityStatus, LinkedIdentity
from src.domain.integrations.entities import (
    ChannelInstallation,
    ChannelType,
    InstallationStatus,
)
from src.domain.work_items.entities import ProjectRef
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import ChannelDeliveryFailedError, NotFoundError
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeChannelInstallationRepository,
    FakeDelegatedCredentialRepository,
    FakeGroupBindingRepository,
    FakePromInstanceRepository,
)
from tests.fakes.support import FakeClock, FakePromApiClient, FakeSecretProvider

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def _payload(
    action: str, *, activity: dict[str, Any] | None = None, state_group: str = "started"
) -> dict[str, Any]:
    return {
        "event": "issue",
        "action": action,
        "data": {
            "id": "wi-1",
            "name": "Fix login",
            "sequence_id": 12,
            "project": "p1",
            "state": {"name": "Done", "group": state_group},
            "assignees": [
                {"id": "u-nok", "display_name": "Nok"},
                {"id": "u-som", "display_name": "Somchai"},
            ],
        },
        "activity": {"actor": {"display_name": "Praew"}, **(activity or {})},
    }


# --- group_events_for ------------------------------------------------------------------------


def test_created_event_lists_assignees() -> None:
    [event] = group_events_for(_payload("created"))
    assert event.event_type is GroupEventType.CREATED
    assert event.assignee_names == ["Nok", "Somchai"] and event.actor_name == "Praew"
    assert event.sequence_id == 12 and event.project_id == "p1"


def test_assignment_names_only_newly_added() -> None:
    payload = _payload(
        "updated",
        activity={"field": "assignee_ids", "old_value": ["u-nok"], "new_value": ["u-nok", "u-som"]},
    )
    [event] = group_events_for(payload)
    assert event.event_type is GroupEventType.ASSIGNED and event.assignee_names == ["Somchai"]


def test_completed_only_when_state_group_completed() -> None:
    done = _payload("updated", activity={"field": "state_id"}, state_group="completed")
    assert [e.event_type for e in group_events_for(done)] == [GroupEventType.COMPLETED]
    started = _payload("updated", activity={"field": "state_id"}, state_group="started")
    assert group_events_for(started) == []


def test_unrelated_payloads_give_nothing() -> None:
    assert group_events_for({"event": "issue_comment", "action": "created", "data": {}}) == []
    assert group_events_for(_payload("updated", activity={"field": "priority"})) == []
    removed = _payload(
        "updated", activity={"field": "assignee_ids", "old_value": ["u-nok"], "new_value": []}
    )
    assert group_events_for(removed) == []


def test_copy() -> None:
    [event] = group_events_for(
        _payload("updated", activity={"field": "state_id"}, state_group="completed")
    )
    binding = _binding(project_id="p1", project_name="AI Platform")
    assert (
        compose_group_event(event, binding)
        == "✅ งานเสร็จแล้ว: #12 Fix login\nโดย Praew · AI Platform"
    )


# --- GroupNotifier ---------------------------------------------------------------------------


@dataclass
class FakeGroupPublisher:
    sent: list[tuple[ChannelType, str, str]] = field(default_factory=list)
    fail_groups: set[str] = field(default_factory=set)

    async def send_group_message(
        self, channel_type: ChannelType, credentials: dict[str, str], group_id: str, text: str
    ) -> None:
        if group_id in self.fail_groups:
            raise ChannelDeliveryFailedError("nope")
        self.sent.append((channel_type, group_id, text))


def _binding(
    *,
    installation_id: Any = None,
    group_id: str = "G1",
    workspace_id: Any = None,
    project_id: str | None = None,
    project_name: str | None = None,
    events: set[GroupEventType] | None = None,
) -> GroupBinding:
    return GroupBinding(
        id=uuid4(),
        customer_id=uuid4(),
        channel_installation_id=installation_id or uuid4(),
        channel_type=ChannelType.LINE,
        group_id=group_id,
        workspace_connection_id=workspace_id or uuid4(),
        project_id=project_id,
        project_name=project_name,
        bound_by_prom_user_id="u-me",
        events=events if events is not None else {GroupEventType.COMPLETED},
        created_at=NOW,
        updated_at=NOW,
    )


def _workspace() -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid4(),
        customer_id=uuid4(),
        prom_instance_id=uuid4(),
        prom_workspace_id="w",
        prom_workspace_slug="acme",
        name="Acme",
        created_at=NOW,
        updated_at=NOW,
    )


async def _installation(
    repo: FakeChannelInstallationRepository, secrets: FakeSecretProvider, *, active: bool = True
) -> ChannelInstallation:
    installation = ChannelInstallation(
        id=uuid4(),
        customer_id=uuid4(),
        channel_type=ChannelType.LINE,
        display_name="LINE",
        status=InstallationStatus.ACTIVE if active else InstallationStatus.REVOKED,
        external_account_id=f"@{uuid4().hex[:6]}",
        encrypted_credentials=secrets.encrypt(json.dumps({"channel_access_token": "t"}).encode()),
        default_workspace_connection_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.add(installation)
    return installation


@pytest.mark.asyncio
async def test_notifier_fans_out_by_scope_and_switches() -> None:
    secrets = FakeSecretProvider()
    installs = FakeChannelInstallationRepository()
    active = await _installation(installs, secrets)
    revoked = await _installation(installs, secrets, active=False)
    ws = _workspace()
    bindings = FakeGroupBindingRepository()
    await bindings.save(_binding(installation_id=active.id, group_id="ALL", workspace_id=ws.id))
    await bindings.save(
        _binding(installation_id=active.id, group_id="P1", workspace_id=ws.id, project_id="p1")
    )
    await bindings.save(
        _binding(installation_id=active.id, group_id="P2", workspace_id=ws.id, project_id="p2")
    )
    await bindings.save(
        _binding(
            installation_id=active.id,
            group_id="NEW-ONLY",
            workspace_id=ws.id,
            events={GroupEventType.CREATED},
        )
    )
    await bindings.save(
        _binding(installation_id=revoked.id, group_id="REVOKED", workspace_id=ws.id)
    )
    await bindings.save(_binding(installation_id=active.id, group_id="BROKEN", workspace_id=ws.id))
    await bindings.save(_binding(installation_id=active.id, group_id="OTHER-WS"))
    publisher = FakeGroupPublisher(fail_groups={"BROKEN"})
    audit = FakeAuditLogRepository()
    notifier = GroupNotifier(
        bindings=bindings,
        installations=installs,
        secrets_provider=secrets,
        publisher=publisher,
        compose=compose_group_event,
        audit_log=audit,
        clock=FakeClock(NOW),
    )

    sent = await notifier.notify(
        ws, _payload("updated", activity={"field": "state_id"}, state_group="completed"), "c"
    )

    assert sent == 2
    assert sorted(g for _t, g, _x in publisher.sent) == ["ALL", "P1"]
    assert [e.action for e in audit.entries].count("group.notify_failed") == 1


# --- GroupBindingService ---------------------------------------------------------------------


async def _service() -> tuple[
    GroupBindingService,
    FakePromApiClient,
    LinkedIdentity,
    ChannelInstallation,
    WorkspaceConnection,
    FakeGroupBindingRepository,
]:
    secrets = FakeSecretProvider()
    instances = FakePromInstanceRepository()
    from src.domain.instances.entities import InstanceStatus, PromInstance

    instance = PromInstance(
        id=uuid4(),
        customer_id=uuid4(),
        name="P",
        base_url="https://prom.example.com",
        status=InstanceStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    await instances.add(instance)
    creds = FakeDelegatedCredentialRepository()
    await creds.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=instance.customer_id,
            prom_instance_id=instance.id,
            prom_user_id="u-me",
            encrypted_token=secrets.encrypt(b"t"),
            created_at=NOW,
            updated_at=NOW,
            rotated_at=None,
        )
    )
    api = FakePromApiClient(projects=[ProjectRef(id="p1", name="AI Platform", identifier="AI")])
    installs = FakeChannelInstallationRepository()
    installation = await _installation(installs, secrets)
    identity = LinkedIdentity(
        id=uuid4(),
        customer_id=installation.customer_id,
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="U1",
        prom_instance_id=instance.id,
        prom_user_id="u-me",
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=NOW,
        unlinked_at=None,
    )
    bindings = FakeGroupBindingRepository()
    clock = FakeClock(NOW)
    audit = FakeAuditLogRepository()
    service = GroupBindingService(
        bindings=bindings,
        list_projects=ListProjects(
            prom_instances=instances,
            delegated_credentials=creds,
            secrets_provider=secrets,
            prom_api_client=api,
            audit_log=audit,
            clock=clock,
        ),
        audit_log=audit,
        clock=clock,
    )
    return service, api, identity, installation, _workspace(), bindings


@pytest.mark.asyncio
async def test_bind_toggle_rebind_unbind() -> None:
    service, _api, identity, installation, workspace, bindings = await _service()

    binding = await service.bind(
        installation=installation,
        group_id="G",
        identity=identity,
        workspace=workspace,
        project_id="p1",
        correlation_id="c",
    )
    assert binding.project_name == "AI Platform" and binding.events == {GroupEventType.COMPLETED}

    toggled = await service.toggle(
        installation=installation,
        group_id="G",
        identity=identity,
        event_value="created",
        correlation_id="c",
    )
    assert toggled is not None and toggled.events == {
        GroupEventType.COMPLETED,
        GroupEventType.CREATED,
    }
    forged = await service.toggle(
        installation=installation,
        group_id="G",
        identity=identity,
        event_value="everything",
        correlation_id="c",
    )
    assert forged is not None and forged.events == toggled.events

    rebound = await service.bind(
        installation=installation,
        group_id="G",
        identity=identity,
        workspace=workspace,
        project_id=None,
        correlation_id="c",
    )
    assert rebound.id == binding.id and rebound.project_id is None
    assert rebound.events == {GroupEventType.COMPLETED, GroupEventType.CREATED}  # kept

    assert await service.unbind(
        installation=installation, group_id="G", identity=identity, correlation_id="c"
    )
    assert await bindings.get_by_group(installation.id, "G") is None
    assert not await service.unbind(
        installation=installation, group_id="G", identity=identity, correlation_id="c"
    )


@pytest.mark.asyncio
async def test_cannot_bind_a_project_the_user_cannot_see() -> None:
    service, _api, identity, installation, workspace, _ = await _service()
    with pytest.raises(NotFoundError):
        await service.bind(
            installation=installation,
            group_id="G",
            identity=identity,
            workspace=workspace,
            project_id="someone-elses-project",
            correlation_id="c",
        )


@pytest.mark.asyncio
async def test_webhook_processing_posts_to_bound_group(notifications_harness) -> None:  # type: ignore[no-untyped-def]
    """ProcessPromWebhookEvent → GroupNotifier wiring, through the same harness the Gate 7
    tests use."""
    h = notifications_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    workspace = await h.seed_workspace_connection(customer.id, instance.id)
    installation = await h.seed_installation(customer.id, ChannelType.LINE)
    bindings = FakeGroupBindingRepository()
    await bindings.save(
        _binding(installation_id=installation.id, group_id="TEAM", workspace_id=workspace.id)
    )
    publisher = FakeGroupPublisher()
    use_case = h.process_webhook_event
    use_case.group_notifier = GroupNotifier(
        bindings=bindings,
        installations=h.installations,
        secrets_provider=h.secrets_provider,
        publisher=publisher,
        compose=compose_group_event,
        audit_log=h.audit_log,
        clock=h.clock,
    )
    await use_case.execute(
        workspace_connection_id=workspace.id,
        payload=_payload("updated", activity={"field": "state_id"}, state_group="completed"),
        correlation_id="c",
    )
    assert [g for _t, g, _x in publisher.sent] == ["TEAM"]
