"""Shared fixture bundling every fake port plus factory helpers for seeding domain entities,
used across tests/application/test_account_linking.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from src.application.account_linking.use_cases import (
    ActivateInstallation,
    ConsumeLinkingCode,
    CreateChannelInstallation,
    DeactivateInstallation,
    LinkInstallationToWorkspace,
    RevokeInstallation,
    StartAccountLinking,
    TestChannelConnection,
    UnlinkIdentity,
    UnlinkWorkspaceFromInstallation,
)
from src.application.notifications.use_cases import (
    CheckDueDateNotifications,
    FlushOutboundMessage,
    ProcessPromWebhookEvent,
    SendPrivateNotification,
)
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    GetWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListStates,
    MarkComplete,
    SearchWorkItems,
    UpdateWorkItemState,
)
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import DelegatedCredential, IdentityStatus, LinkedIdentity
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationStatus
from src.domain.workspaces.entities import WorkspaceConnection
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeChannelInstallationRepository,
    FakeCustomerRepository,
    FakeDelegatedCredentialRepository,
    FakeLinkedIdentityRepository,
    FakeLinkingCodeRepository,
    FakeNotificationPreferenceRepository,
    FakeOutboundMessageRepository,
    FakePromInstanceRepository,
    FakeWorkspaceConnectionRepository,
)
from tests.fakes.support import (
    FakeChannelCredentialValidator,
    FakeClock,
    FakeMessagePublisher,
    FakePromApiClient,
    FakeRateLimiter,
    FakeSecretProvider,
    FakeWebhookDedupStore,
)

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class AccountLinkingHarness:
    customers: FakeCustomerRepository
    prom_instances: FakePromInstanceRepository
    workspace_connections: FakeWorkspaceConnectionRepository
    installations: FakeChannelInstallationRepository
    linked_identities: FakeLinkedIdentityRepository
    linking_codes: FakeLinkingCodeRepository
    delegated_credentials: FakeDelegatedCredentialRepository
    audit_log: FakeAuditLogRepository
    clock: FakeClock
    validator: FakeChannelCredentialValidator
    prom_api_client: FakePromApiClient
    secrets_provider: FakeSecretProvider
    rate_limiter: FakeRateLimiter

    @property
    def create_installation(self) -> CreateChannelInstallation:
        return CreateChannelInstallation(
            installations=self.installations,
            validator=self.validator,
            secrets_provider=self.secrets_provider,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def link_workspace(self) -> LinkInstallationToWorkspace:
        return LinkInstallationToWorkspace(
            installations=self.installations,
            workspace_connections=self.workspace_connections,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def start_linking(self) -> StartAccountLinking:
        return StartAccountLinking(
            installations=self.installations,
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            linking_codes=self.linking_codes,
            prom_api_client=self.prom_api_client,
            secrets_provider=self.secrets_provider,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def consume_code(self) -> ConsumeLinkingCode:
        return ConsumeLinkingCode(
            installations=self.installations,
            linking_codes=self.linking_codes,
            linked_identities=self.linked_identities,
            rate_limiter=self.rate_limiter,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def unlink_identity(self) -> UnlinkIdentity:
        return UnlinkIdentity(
            linked_identities=self.linked_identities, audit_log=self.audit_log, clock=self.clock
        )

    @property
    def revoke_installation(self) -> RevokeInstallation:
        return RevokeInstallation(
            installations=self.installations, audit_log=self.audit_log, clock=self.clock
        )

    @property
    def unlink_workspace(self) -> UnlinkWorkspaceFromInstallation:
        return UnlinkWorkspaceFromInstallation(
            installations=self.installations, audit_log=self.audit_log, clock=self.clock
        )

    @property
    def test_connection(self) -> TestChannelConnection:
        return TestChannelConnection(
            installations=self.installations,
            validator=self.validator,
            secrets_provider=self.secrets_provider,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def activate_installation(self) -> ActivateInstallation:
        return ActivateInstallation(
            installations=self.installations, audit_log=self.audit_log, clock=self.clock
        )

    @property
    def deactivate_installation(self) -> DeactivateInstallation:
        return DeactivateInstallation(
            installations=self.installations, audit_log=self.audit_log, clock=self.clock
        )

    async def seed_customer(self) -> Customer:
        customer = Customer(
            id=uuid4(),
            name="Acme Corp",
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

    async def create_active_installation(
        self, customer_id: UUID, channel_type: ChannelType = ChannelType.LINE
    ) -> ChannelInstallation:
        return await self.create_installation.execute(
            customer_id=customer_id,
            channel_type=channel_type,
            display_name="Acme LINE Bot",
            credentials={"channel_access_token": "token-123"},
            correlation_id="test-correlation-id",
        )


@pytest.fixture
def harness() -> AccountLinkingHarness:
    return AccountLinkingHarness(
        customers=FakeCustomerRepository(),
        prom_instances=FakePromInstanceRepository(),
        workspace_connections=FakeWorkspaceConnectionRepository(),
        installations=FakeChannelInstallationRepository(),
        linked_identities=FakeLinkedIdentityRepository(),
        linking_codes=FakeLinkingCodeRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(_FIXED_NOW),
        validator=FakeChannelCredentialValidator(),
        prom_api_client=FakePromApiClient(),
        secrets_provider=FakeSecretProvider(),
        rate_limiter=FakeRateLimiter(),
    )


@dataclass
class WorkItemsHarness:
    """Gate 4 counterpart to AccountLinkingHarness — used by
    tests/application/test_work_items.py. Every work-item use case shares the same five ports
    (prom_instances, delegated_credentials, secrets_provider, prom_api_client, audit_log, clock),
    so one `@property` per use case here is enough (no per-test wiring boilerplate)."""

    prom_instances: FakePromInstanceRepository
    delegated_credentials: FakeDelegatedCredentialRepository
    audit_log: FakeAuditLogRepository
    clock: FakeClock
    prom_api_client: FakePromApiClient
    secrets_provider: FakeSecretProvider

    @property
    def create_work_item(self) -> CreateWorkItem:
        return CreateWorkItem(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def get_work_item(self) -> GetWorkItem:
        return GetWorkItem(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def search_work_items(self) -> SearchWorkItems:
        return SearchWorkItems(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def list_projects(self) -> ListProjects:
        return ListProjects(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def list_states(self) -> ListStates:
        return ListStates(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def list_project_members(self) -> ListProjectMembers:
        return ListProjectMembers(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def list_my_work_items(self) -> ListMyWorkItems:
        return ListMyWorkItems(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def update_work_item_state(self) -> UpdateWorkItemState:
        return UpdateWorkItemState(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def change_assignee(self) -> ChangeAssignee:
        return ChangeAssignee(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def add_comment(self) -> AddComment:
        return AddComment(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def mark_complete(self) -> MarkComplete:
        return MarkComplete(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    async def seed_customer(self) -> Customer:
        """No FakeCustomerRepository here (deliberately) — nothing in the work-item use cases
        looks a customer up by id; customer_id only flows through as data taken from whatever
        DelegatedCredential/ChannelInstallation already carries it. Returns an in-memory Customer
        purely so tests have a real id to pass around, consistent with AccountLinkingHarness's
        seed_customer for readability."""
        return Customer(
            id=uuid4(),
            name="Acme Corp",
            status=CustomerStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )

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

    async def link_user(
        self,
        *,
        customer_id: UUID,
        prom_instance_id: UUID,
        prom_user_id: str,
        token: str = "delegated-token",
    ) -> DelegatedCredential:
        """Seeds a DelegatedCredential directly — work_items tests care about "a user is already
        linked", not re-exercising the OTP flow itself (that's test_account_linking.py's job)."""
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
        return credential


@pytest.fixture
def work_items_harness() -> WorkItemsHarness:
    return WorkItemsHarness(
        prom_instances=FakePromInstanceRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(_FIXED_NOW),
        prom_api_client=FakePromApiClient(),
        secrets_provider=FakeSecretProvider(),
    )


@dataclass
class NotificationsHarness:
    """Gate 7 counterpart to AccountLinkingHarness/WorkItemsHarness — used by
    tests/application/test_notifications.py. `schedule_send` calls are recorded rather than
    actually scheduling anything (ProcessPromWebhookEvent takes it as a plain injected callable
    precisely so tests don't need Celery — see its docstring)."""

    customers: FakeCustomerRepository
    prom_instances: FakePromInstanceRepository
    workspace_connections: FakeWorkspaceConnectionRepository
    installations: FakeChannelInstallationRepository
    linked_identities: FakeLinkedIdentityRepository
    delegated_credentials: FakeDelegatedCredentialRepository
    notification_preferences: FakeNotificationPreferenceRepository
    outbound_messages: FakeOutboundMessageRepository
    audit_log: FakeAuditLogRepository
    clock: FakeClock
    prom_api_client: FakePromApiClient
    secrets_provider: FakeSecretProvider
    publisher: FakeMessagePublisher
    dedup_store: FakeWebhookDedupStore
    scheduled_sends: list[UUID]

    @property
    def process_webhook_event(self) -> ProcessPromWebhookEvent:
        return ProcessPromWebhookEvent(
            workspace_connections=self.workspace_connections,
            linked_identities=self.linked_identities,
            notification_preferences=self.notification_preferences,
            outbound_messages=self.outbound_messages,
            delegated_credentials=self.delegated_credentials,
            prom_instances=self.prom_instances,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            clock=self.clock,
            schedule_send=self.scheduled_sends.append,
        )

    @property
    def flush_outbound_message(self) -> FlushOutboundMessage:
        return FlushOutboundMessage(
            outbound_messages=self.outbound_messages,
            linked_identities=self.linked_identities,
            installations=self.installations,
            secrets_provider=self.secrets_provider,
            publisher=self.publisher,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def send_private_notification(self) -> SendPrivateNotification:
        return SendPrivateNotification(
            installations=self.installations,
            secrets_provider=self.secrets_provider,
            publisher=self.publisher,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def check_due_dates(self) -> CheckDueDateNotifications:
        return CheckDueDateNotifications(
            linked_identities=self.linked_identities,
            workspace_connections=self.workspace_connections,
            list_projects=ListProjects(
                prom_instances=self.prom_instances,
                delegated_credentials=self.delegated_credentials,
                secrets_provider=self.secrets_provider,
                prom_api_client=self.prom_api_client,
                audit_log=self.audit_log,
                clock=self.clock,
            ),
            list_my_work_items=ListMyWorkItems(
                prom_instances=self.prom_instances,
                delegated_credentials=self.delegated_credentials,
                secrets_provider=self.secrets_provider,
                prom_api_client=self.prom_api_client,
                audit_log=self.audit_log,
                clock=self.clock,
            ),
            notification_preferences=self.notification_preferences,
            installations=self.installations,
            secrets_provider=self.secrets_provider,
            publisher=self.publisher,
            audit_log=self.audit_log,
            clock=self.clock,
            dedup_store=self.dedup_store,
        )

    async def seed_customer(self) -> Customer:
        customer = Customer(
            id=uuid4(),
            name="Acme Corp",
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

    async def seed_workspace_connection(
        self, customer_id: UUID, prom_instance_id: UUID
    ) -> WorkspaceConnection:
        workspace = WorkspaceConnection(
            id=uuid4(),
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            prom_workspace_id="ws-1",
            prom_workspace_slug="acme",
            name="Acme Workspace",
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.workspace_connections.add(workspace)
        return workspace

    async def seed_installation(
        self, customer_id: UUID, channel_type: ChannelType = ChannelType.LINE
    ) -> ChannelInstallation:
        installation = ChannelInstallation(
            id=uuid4(),
            customer_id=customer_id,
            channel_type=channel_type,
            display_name="Acme Bot",
            status=InstallationStatus.ACTIVE,
            external_account_id="external-1",
            encrypted_credentials=self.secrets_provider.encrypt(b'{"channel_access_token":"t"}'),
            default_workspace_connection_id=None,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.installations.add(installation)
        return installation

    async def link_user(
        self,
        *,
        customer_id: UUID,
        channel_installation_id: UUID,
        prom_instance_id: UUID,
        prom_user_id: str,
        channel_type: ChannelType = ChannelType.LINE,
        channel_user_id: str,
        default_workspace_connection_id: UUID | None = None,
    ) -> LinkedIdentity:
        identity = LinkedIdentity(
            id=uuid4(),
            customer_id=customer_id,
            channel_installation_id=channel_installation_id,
            channel_type=channel_type,
            channel_user_id=channel_user_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            default_workspace_connection_id=default_workspace_connection_id,
            status=IdentityStatus.ACTIVE,
            linked_at=self.clock.now(),
            unlinked_at=None,
        )
        await self.linked_identities.add(identity)
        return identity


@pytest.fixture
def notifications_harness() -> NotificationsHarness:
    return NotificationsHarness(
        customers=FakeCustomerRepository(),
        prom_instances=FakePromInstanceRepository(),
        workspace_connections=FakeWorkspaceConnectionRepository(),
        installations=FakeChannelInstallationRepository(),
        linked_identities=FakeLinkedIdentityRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        notification_preferences=FakeNotificationPreferenceRepository(),
        outbound_messages=FakeOutboundMessageRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(_FIXED_NOW),
        prom_api_client=FakePromApiClient(),
        secrets_provider=FakeSecretProvider(),
        publisher=FakeMessagePublisher(),
        dedup_store=FakeWebhookDedupStore(),
        scheduled_sends=[],
    )
