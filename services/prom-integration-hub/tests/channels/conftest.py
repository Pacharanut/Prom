"""Harness for LineWebhookHandler tests — wires every fake port the handler and the flows it
drives need. No infra; the handler itself is framework-adjacent (constructs LINE JSON) but every
port behind it is faked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from src.application.account_linking.ports import SecretProvider
from src.application.account_linking.use_cases import ConsumeLinkingCode, CreateChannelInstallation
from src.application.work_items.conversation import CreateWorkItemFlow, UpdateWorkItemFlow
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListStates,
    MarkComplete,
    UpdateWorkItemState,
)
from src.channels.discord.handler import DiscordWebhookHandler
from src.channels.line.handler import LineWebhookHandler
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import DelegatedCredential, IdentityStatus, LinkedIdentity
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationWorkspace
from src.domain.workspaces.entities import WorkspaceConnection
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeChannelInstallationRepository,
    FakeCustomerRepository,
    FakeDelegatedCredentialRepository,
    FakeLinkedIdentityRepository,
    FakeLinkingCodeRepository,
    FakePromInstanceRepository,
    FakeWorkspaceConnectionRepository,
)
from tests.fakes.support import (
    FakeChannelCredentialValidator,
    FakeClock,
    FakeConversationStateRepository,
    FakePromApiClient,
    FakeRateLimiter,
    FakeSecretProvider,
    FakeWebhookDedupStore,
)

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class LineHandlerHarness:
    customers: FakeCustomerRepository
    prom_instances: FakePromInstanceRepository
    workspace_connections: FakeWorkspaceConnectionRepository
    installations: FakeChannelInstallationRepository
    linked_identities: FakeLinkedIdentityRepository
    linking_codes: FakeLinkingCodeRepository
    delegated_credentials: FakeDelegatedCredentialRepository
    audit_log: FakeAuditLogRepository
    clock: FakeClock
    secrets_provider: SecretProvider
    """Typed as the port, not FakeSecretProvider — test_line_webhook_routes.py substitutes a real
    FernetSecretProvider here since that test exercises the actual route's own (non-overridable)
    get_secret_provider() call, which must decrypt what this harness encrypted."""
    prom_api_client: FakePromApiClient
    rate_limiter: FakeRateLimiter
    dedup_store: FakeWebhookDedupStore
    conversation_states: FakeConversationStateRepository

    @property
    def handler(self) -> LineWebhookHandler:
        list_projects = ListProjects(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        list_states = ListStates(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        list_project_members = ListProjectMembers(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        create_work_item = CreateWorkItem(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        list_my_work_items = ListMyWorkItems(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        update_work_item_state = UpdateWorkItemState(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        change_assignee = ChangeAssignee(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        add_comment = AddComment(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        mark_complete = MarkComplete(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        consume_linking_code = ConsumeLinkingCode(
            installations=self.installations,
            linking_codes=self.linking_codes,
            linked_identities=self.linked_identities,
            rate_limiter=self.rate_limiter,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        return LineWebhookHandler(
            linked_identities=self.linked_identities,
            channel_installations=self.installations,
            workspace_connections=self.workspace_connections,
            delegated_credentials=self.delegated_credentials,
            prom_instances=self.prom_instances,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            consume_linking_code=consume_linking_code,
            dedup_store=self.dedup_store,
            conversation_states=self.conversation_states,
            create_flow=CreateWorkItemFlow(
                list_projects=list_projects,
                list_states=list_states,
                list_project_members=list_project_members,
                create_work_item=create_work_item,
            ),
            update_flow=UpdateWorkItemFlow(
                list_states=list_states,
                list_project_members=list_project_members,
                update_work_item_state=update_work_item_state,
                change_assignee=change_assignee,
                add_comment=add_comment,
                mark_complete=mark_complete,
            ),
            list_projects=list_projects,
            list_my_work_items=list_my_work_items,
        )

    async def seed_customer(self) -> Customer:
        customer = Customer(
            id=uuid4(),
            name="Acme",
            status=CustomerStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.customers.add(customer)
        return customer

    async def seed_prom_instance(self, customer_id: UUID) -> PromInstance:
        instance = PromInstance(
            id=uuid4(),
            customer_id=customer_id,
            name="Acme Prom",
            base_url="https://prom.acme.example",
            status=InstanceStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.prom_instances.add(instance)
        return instance

    async def seed_workspace(
        self, customer_id: UUID, prom_instance_id: UUID
    ) -> WorkspaceConnection:
        # prom_workspace_id must be unique per (prom_instance_id, prom_workspace_id) — derived
        # from a fresh uuid4 rather than a hardcoded "ws-1" so two seed_workspace() calls in the
        # same test produce two distinct workspaces, not a get-or-create collision (Gate 8's
        # WorkspaceConnectionRepository.add is now a real get-or-create on that pair).
        workspace_id = uuid4()
        workspace = WorkspaceConnection(
            id=workspace_id,
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            prom_workspace_id=f"ws-{workspace_id.hex[:8]}",
            prom_workspace_slug="acme",
            name="Acme Workspace",
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.workspace_connections.add(workspace)
        return workspace

    async def seed_installation(
        self,
        customer_id: UUID,
        *,
        channel_secret: str = "shh",
        channel_access_token: str = "token-1",
    ) -> ChannelInstallation:
        validator = FakeChannelCredentialValidator()
        installation = await CreateChannelInstallation(
            installations=self.installations,
            validator=validator,
            secrets_provider=self.secrets_provider,
            audit_log=self.audit_log,
            clock=self.clock,
        ).execute(
            customer_id=customer_id,
            channel_type=ChannelType.LINE,
            display_name="Acme LINE Bot",
            credentials={
                "channel_access_token": channel_access_token,
                "channel_secret": channel_secret,
            },
            correlation_id="seed",
        )
        return installation

    async def link_workspace(self, installation_id: UUID, workspace_connection_id: UUID) -> None:
        await self.installations.add_workspace_link(
            InstallationWorkspace(
                id=uuid4(),
                channel_installation_id=installation_id,
                workspace_connection_id=workspace_connection_id,
                created_at=self.clock.now(),
            )
        )

    async def link_user(
        self,
        *,
        customer_id: UUID,
        installation_id: UUID,
        prom_instance_id: UUID,
        prom_user_id: str,
        channel_user_id: str,
        default_workspace_connection_id: UUID | None,
        token: str = "delegated-token",
    ) -> None:
        now = self.clock.now()
        credential = DelegatedCredential(
            id=uuid4(),
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            encrypted_token=self.secrets_provider.encrypt(token.encode("utf-8")),
            created_at=now,
            updated_at=now,
            rotated_at=None,
        )
        await self.delegated_credentials.upsert(credential)

        identity = LinkedIdentity(
            id=uuid4(),
            customer_id=customer_id,
            channel_installation_id=installation_id,
            channel_type=ChannelType.LINE,
            channel_user_id=channel_user_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            default_workspace_connection_id=default_workspace_connection_id,
            status=IdentityStatus.ACTIVE,
            linked_at=now,
            unlinked_at=None,
        )
        await self.linked_identities.add(identity)


@dataclass
class DiscordHandlerHarness:
    """Discord counterpart to LineHandlerHarness — same fake ports, same seed helpers, wires a
    DiscordWebhookHandler instead of a LineWebhookHandler. Deliberately not a subclass: the two
    harnesses' seed_installation/link_user differ in channel_type and credentials shape, and
    keeping them as separate, fully-readable classes (mirroring components.py's decision not to
    share code with messages.py) is clearer than a shared base with channel-specific overrides for
    a handful of short methods.
    """

    customers: FakeCustomerRepository
    prom_instances: FakePromInstanceRepository
    workspace_connections: FakeWorkspaceConnectionRepository
    installations: FakeChannelInstallationRepository
    linked_identities: FakeLinkedIdentityRepository
    linking_codes: FakeLinkingCodeRepository
    delegated_credentials: FakeDelegatedCredentialRepository
    audit_log: FakeAuditLogRepository
    clock: FakeClock
    secrets_provider: SecretProvider
    prom_api_client: FakePromApiClient
    rate_limiter: FakeRateLimiter
    dedup_store: FakeWebhookDedupStore
    conversation_states: FakeConversationStateRepository

    @property
    def handler(self) -> DiscordWebhookHandler:
        list_projects = ListProjects(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        list_states = ListStates(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        list_project_members = ListProjectMembers(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        create_work_item = CreateWorkItem(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        list_my_work_items = ListMyWorkItems(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        update_work_item_state = UpdateWorkItemState(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        change_assignee = ChangeAssignee(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        add_comment = AddComment(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        mark_complete = MarkComplete(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        consume_linking_code = ConsumeLinkingCode(
            installations=self.installations,
            linking_codes=self.linking_codes,
            linked_identities=self.linked_identities,
            rate_limiter=self.rate_limiter,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        return DiscordWebhookHandler(
            linked_identities=self.linked_identities,
            channel_installations=self.installations,
            workspace_connections=self.workspace_connections,
            delegated_credentials=self.delegated_credentials,
            prom_instances=self.prom_instances,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            consume_linking_code=consume_linking_code,
            dedup_store=self.dedup_store,
            conversation_states=self.conversation_states,
            create_flow=CreateWorkItemFlow(
                list_projects=list_projects,
                list_states=list_states,
                list_project_members=list_project_members,
                create_work_item=create_work_item,
            ),
            update_flow=UpdateWorkItemFlow(
                list_states=list_states,
                list_project_members=list_project_members,
                update_work_item_state=update_work_item_state,
                change_assignee=change_assignee,
                add_comment=add_comment,
                mark_complete=mark_complete,
            ),
            list_projects=list_projects,
            list_my_work_items=list_my_work_items,
        )

    async def seed_customer(self) -> Customer:
        customer = Customer(
            id=uuid4(),
            name="Acme",
            status=CustomerStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.customers.add(customer)
        return customer

    async def seed_prom_instance(self, customer_id: UUID) -> PromInstance:
        instance = PromInstance(
            id=uuid4(),
            customer_id=customer_id,
            name="Acme Prom",
            base_url="https://prom.acme.example",
            status=InstanceStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.prom_instances.add(instance)
        return instance

    async def seed_workspace(
        self, customer_id: UUID, prom_instance_id: UUID
    ) -> WorkspaceConnection:
        # prom_workspace_id must be unique per (prom_instance_id, prom_workspace_id) — derived
        # from a fresh uuid4 rather than a hardcoded "ws-1" so two seed_workspace() calls in the
        # same test produce two distinct workspaces, not a get-or-create collision (Gate 8's
        # WorkspaceConnectionRepository.add is now a real get-or-create on that pair).
        workspace_id = uuid4()
        workspace = WorkspaceConnection(
            id=workspace_id,
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            prom_workspace_id=f"ws-{workspace_id.hex[:8]}",
            prom_workspace_slug="acme",
            name="Acme Workspace",
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.workspace_connections.add(workspace)
        return workspace

    async def seed_installation(
        self, customer_id: UUID, *, bot_token: str = "bot-token-1"
    ) -> ChannelInstallation:
        validator = FakeChannelCredentialValidator()
        installation = await CreateChannelInstallation(
            installations=self.installations,
            validator=validator,
            secrets_provider=self.secrets_provider,
            audit_log=self.audit_log,
            clock=self.clock,
        ).execute(
            customer_id=customer_id,
            channel_type=ChannelType.DISCORD,
            display_name="Acme Discord Bot",
            credentials={"bot_token": bot_token},
            correlation_id="seed",
        )
        return installation

    async def link_workspace(self, installation_id: UUID, workspace_connection_id: UUID) -> None:
        await self.installations.add_workspace_link(
            InstallationWorkspace(
                id=uuid4(),
                channel_installation_id=installation_id,
                workspace_connection_id=workspace_connection_id,
                created_at=self.clock.now(),
            )
        )

    async def link_user(
        self,
        *,
        customer_id: UUID,
        installation_id: UUID,
        prom_instance_id: UUID,
        prom_user_id: str,
        channel_user_id: str,
        default_workspace_connection_id: UUID | None,
        token: str = "delegated-token",
    ) -> None:
        now = self.clock.now()
        credential = DelegatedCredential(
            id=uuid4(),
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            encrypted_token=self.secrets_provider.encrypt(token.encode("utf-8")),
            created_at=now,
            updated_at=now,
            rotated_at=None,
        )
        await self.delegated_credentials.upsert(credential)

        identity = LinkedIdentity(
            id=uuid4(),
            customer_id=customer_id,
            channel_installation_id=installation_id,
            channel_type=ChannelType.DISCORD,
            channel_user_id=channel_user_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            default_workspace_connection_id=default_workspace_connection_id,
            status=IdentityStatus.ACTIVE,
            linked_at=now,
            unlinked_at=None,
        )
        await self.linked_identities.add(identity)


@pytest.fixture
def discord_harness() -> DiscordHandlerHarness:
    return DiscordHandlerHarness(
        customers=FakeCustomerRepository(),
        prom_instances=FakePromInstanceRepository(),
        workspace_connections=FakeWorkspaceConnectionRepository(),
        installations=FakeChannelInstallationRepository(),
        linked_identities=FakeLinkedIdentityRepository(),
        linking_codes=FakeLinkingCodeRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(_FIXED_NOW),
        secrets_provider=FakeSecretProvider(),
        prom_api_client=FakePromApiClient(),
        rate_limiter=FakeRateLimiter(),
        dedup_store=FakeWebhookDedupStore(),
        conversation_states=FakeConversationStateRepository(),
    )


@pytest.fixture
def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, list[dict]]]:
    """Intercepts every reply LineWebhookHandler sends (it calls src.channels.line.handler's
    imported `reply_message` directly) so tests can assert on what was sent without a real
    network call and without needing httpx.MockTransport plumbed through every test."""
    calls: list[tuple[str, str, list[dict]]] = []

    async def fake_reply(
        channel_access_token: str, reply_token: str, messages: list[dict], *, client: object = None
    ) -> None:
        calls.append((channel_access_token, reply_token, messages))

    monkeypatch.setattr("src.channels.line.handler.reply_message", fake_reply)
    return calls


@pytest.fixture
def line_harness() -> LineHandlerHarness:
    return LineHandlerHarness(
        customers=FakeCustomerRepository(),
        prom_instances=FakePromInstanceRepository(),
        workspace_connections=FakeWorkspaceConnectionRepository(),
        installations=FakeChannelInstallationRepository(),
        linked_identities=FakeLinkedIdentityRepository(),
        linking_codes=FakeLinkingCodeRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(_FIXED_NOW),
        secrets_provider=FakeSecretProvider(),
        prom_api_client=FakePromApiClient(),
        rate_limiter=FakeRateLimiter(),
        dedup_store=FakeWebhookDedupStore(),
        conversation_states=FakeConversationStateRepository(),
    )
