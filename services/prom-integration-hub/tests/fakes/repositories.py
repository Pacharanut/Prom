"""In-memory fake implementations of every repository port in
src/application/account_linking/ports.py and src/application/work_items/ports.py.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from src.application.account_linking.ports import (
    AuditLogFilters,
    AuditLogRepository,
    ChannelInstallationRepository,
    CustomerRepository,
    DelegatedCredentialRepository,
    LinkedIdentityRepository,
    LinkingCodeRepository,
    PromInstanceRepository,
    WorkspaceConnectionRepository,
)
from src.application.ai.ports import AiSettingsRepository
from src.application.notifications.ports import (
    NotificationPreferenceRepository,
    OutboundMessageRepository,
)
from src.application.webhooks.ports import WebhookEventRepository
from src.application.work_items.ports import IdempotencyRepository
from src.domain.ai.entities import AiSettings
from src.domain.audit.entities import AuditLogEntry
from src.domain.classification.entities import ClassificationSettings
from src.domain.customers.entities import Customer
from src.domain.groups.entities import GroupBinding
from src.domain.idempotency.entities import IdempotencyRecord
from src.domain.identities.entities import (
    DelegatedCredential,
    IdentityStatus,
    LinkedIdentity,
    LinkingCode,
)
from src.domain.instances.entities import PromInstance
from src.domain.integrations.entities import (
    ChannelInstallation,
    ChannelType,
    InstallationStatus,
    InstallationWorkspace,
)
from src.domain.notifications.entities import (
    NotificationEventType,
    OutboundMessageRecord,
    OutboundMessageStatus,
)
from src.domain.webhooks.entities import WebhookEventRecord
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import IdentityAlreadyLinkedError


class FakeCustomerRepository(CustomerRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, Customer] = {}

    async def get(self, customer_id: UUID) -> Customer | None:
        return self._items.get(customer_id)

    async def add(self, customer: Customer) -> None:
        self._items[customer.id] = customer


class FakePromInstanceRepository(PromInstanceRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, PromInstance] = {}

    async def get(self, instance_id: UUID) -> PromInstance | None:
        return self._items.get(instance_id)

    async def add(self, instance: PromInstance) -> None:
        self._items[instance.id] = instance


class FakeWorkspaceConnectionRepository(WorkspaceConnectionRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, WorkspaceConnection] = {}

    async def get(self, workspace_connection_id: UUID) -> WorkspaceConnection | None:
        return self._items.get(workspace_connection_id)

    async def add(
        self, workspace_connection: WorkspaceConnection
    ) -> tuple[WorkspaceConnection, bool]:
        """Mirrors PostgresWorkspaceConnectionRepository.add's get-or-create-and-update
        behavior against the real unique key (prom_instance_id, prom_workspace_id)."""
        existing = await self.get_by_external_id(
            workspace_connection.prom_instance_id, workspace_connection.prom_workspace_id
        )
        if existing is not None:
            existing.name = workspace_connection.name
            existing.prom_workspace_slug = workspace_connection.prom_workspace_slug
            existing.updated_at = workspace_connection.updated_at
            return existing, False
        self._items[workspace_connection.id] = workspace_connection
        return workspace_connection, True

    async def get_by_external_id(
        self, prom_instance_id: UUID, prom_workspace_id: str
    ) -> WorkspaceConnection | None:
        for item in self._items.values():
            if (
                item.prom_instance_id == prom_instance_id
                and item.prom_workspace_id == prom_workspace_id
            ):
                return item
        return None

    async def save(self, workspace_connection: WorkspaceConnection) -> None:
        self._items[workspace_connection.id] = workspace_connection


class FakeChannelInstallationRepository(ChannelInstallationRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, ChannelInstallation] = {}
        self._links: dict[UUID, InstallationWorkspace] = {}

    async def get(self, installation_id: UUID) -> ChannelInstallation | None:
        return self._items.get(installation_id)

    async def add(self, installation: ChannelInstallation) -> None:
        self._items[installation.id] = installation

    async def save(self, installation: ChannelInstallation) -> None:
        self._items[installation.id] = installation

    async def add_workspace_link(self, link: InstallationWorkspace) -> None:
        self._links[link.id] = link

    async def get_workspace_link(
        self, channel_installation_id: UUID, workspace_connection_id: UUID
    ) -> InstallationWorkspace | None:
        for link in self._links.values():
            if (
                link.channel_installation_id == channel_installation_id
                and link.workspace_connection_id == workspace_connection_id
            ):
                return link
        return None

    async def list_workspaces(self, channel_installation_id: UUID) -> list[InstallationWorkspace]:
        return [
            link
            for link in self._links.values()
            if link.channel_installation_id == channel_installation_id
        ]

    async def list_active_by_channel_type(
        self, channel_type: ChannelType
    ) -> list[ChannelInstallation]:
        return [
            installation
            for installation in self._items.values()
            if installation.channel_type == channel_type
            and installation.status == InstallationStatus.ACTIVE
        ]

    async def list_by_customer(self, customer_id: UUID) -> list[ChannelInstallation]:
        return [
            installation
            for installation in self._items.values()
            if installation.customer_id == customer_id
        ]

    async def remove_workspace_link(
        self, channel_installation_id: UUID, workspace_connection_id: UUID
    ) -> bool:
        for link_id, link in list(self._links.items()):
            if (
                link.channel_installation_id == channel_installation_id
                and link.workspace_connection_id == workspace_connection_id
            ):
                del self._links[link_id]
                return True
        return False

    async def delete(self, installation_id: UUID) -> None:
        self._items.pop(installation_id, None)
        for link_id, link in list(self._links.items()):
            if link.channel_installation_id == installation_id:
                del self._links[link_id]


class FakeLinkedIdentityRepository(LinkedIdentityRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, LinkedIdentity] = {}

    async def get(self, linked_identity_id: UUID) -> LinkedIdentity | None:
        return self._items.get(linked_identity_id)

    async def add(self, identity: LinkedIdentity) -> None:
        """Mirrors the real Postgres repository's partial-unique-index behavior (see
        `PostgresLinkedIdentityRepository.add`): reject a second *active* identity for the same
        channel_user_id or prom_user_id on the same installation.
        """
        for existing in self._items.values():
            if (
                existing.channel_installation_id == identity.channel_installation_id
                and existing.status == IdentityStatus.ACTIVE
                and (
                    existing.channel_user_id == identity.channel_user_id
                    or existing.prom_user_id == identity.prom_user_id
                )
            ):
                raise IdentityAlreadyLinkedError(
                    "an active linked identity already exists for this channel user or Prom "
                    "user on this installation"
                )
        self._items[identity.id] = identity

    async def save(self, identity: LinkedIdentity) -> None:
        self._items[identity.id] = identity

    async def get_active_by_channel_user(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> LinkedIdentity | None:
        for item in self._items.values():
            if (
                item.channel_installation_id == channel_installation_id
                and item.channel_user_id == channel_user_id
                and item.status == IdentityStatus.ACTIVE
            ):
                return item
        return None

    async def list_active_by_prom_user(
        self, prom_instance_id: UUID, prom_user_id: str
    ) -> list[LinkedIdentity]:
        return [
            item
            for item in self._items.values()
            if item.prom_instance_id == prom_instance_id
            and item.prom_user_id == prom_user_id
            and item.status == IdentityStatus.ACTIVE
        ]

    async def list_all_active(self) -> list[LinkedIdentity]:
        return [item for item in self._items.values() if item.status == IdentityStatus.ACTIVE]

    async def list_all_active_with_default_workspace(self) -> list[LinkedIdentity]:
        return [
            item
            for item in self._items.values()
            if item.status == IdentityStatus.ACTIVE
            and item.default_workspace_connection_id is not None
        ]

    async def exists_for_installation(self, channel_installation_id: UUID) -> bool:
        return any(
            item.channel_installation_id == channel_installation_id for item in self._items.values()
        )


class FakeLinkingCodeRepository(LinkingCodeRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, LinkingCode] = {}

    async def add(self, linking_code: LinkingCode) -> None:
        self._items[linking_code.id] = linking_code

    async def save(self, linking_code: LinkingCode) -> None:
        self._items[linking_code.id] = linking_code

    async def get_by_code(self, channel_installation_id: UUID, code: str) -> LinkingCode | None:
        for item in self._items.values():
            if item.channel_installation_id == channel_installation_id and item.code == code:
                return item
        return None

    async def list_active_codes(self, channel_installation_id: UUID) -> list[str]:
        return [
            item.code
            for item in self._items.values()
            if item.channel_installation_id == channel_installation_id and item.used_at is None
        ]

    async def get_active_for_user(
        self, channel_installation_id: UUID, prom_user_id: str
    ) -> LinkingCode | None:
        for item in self._items.values():
            if (
                item.channel_installation_id == channel_installation_id
                and item.prom_user_id == prom_user_id
                and item.used_at is None
            ):
                return item
        return None


class FakeDelegatedCredentialRepository(DelegatedCredentialRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str], DelegatedCredential] = {}

    async def get(self, prom_instance_id: UUID, prom_user_id: str) -> DelegatedCredential | None:
        return self._items.get((prom_instance_id, prom_user_id))

    async def get_any_for_instance(self, prom_instance_id: UUID) -> DelegatedCredential | None:
        for (instance_id, _), credential in self._items.items():
            if instance_id == prom_instance_id:
                return credential
        return None

    async def upsert(self, credential: DelegatedCredential) -> None:
        self._items[(credential.prom_instance_id, credential.prom_user_id)] = credential


class FakeAuditLogRepository(AuditLogRepository):
    def __init__(self) -> None:
        self.entries: list[AuditLogEntry] = []

    async def add(self, entry: AuditLogEntry) -> None:
        self.entries.append(entry)

    def last(self) -> AuditLogEntry:
        return self.entries[-1]

    def entries_for_action(self, action: str) -> list[AuditLogEntry]:
        return [entry for entry in self.entries if entry.action == action]

    async def list_for_customer(
        self,
        customer_id: UUID,
        *,
        installation_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        filters: AuditLogFilters | None = None,
    ) -> list[AuditLogEntry]:
        f = filters or AuditLogFilters()
        matches = [
            entry
            for entry in self.entries
            if entry.customer_id == customer_id
            and (installation_id is None or entry.installation_id == installation_id)
            and (f.channel_type is None or entry.channel_type == f.channel_type)
            and (not f.action_contains or f.action_contains in entry.action)
            and (not f.target_id or entry.target_id == f.target_id)
            and (f.result_status is None or entry.result_status == f.result_status)
            and (f.occurred_after is None or entry.occurred_at >= f.occurred_after)
            and (f.occurred_before is None or entry.occurred_at < f.occurred_before)
        ]
        matches.sort(key=lambda e: e.occurred_at, reverse=True)
        return matches[offset : offset + limit]

    async def list_for_target(
        self, target_type: str, target_id: str, *, limit: int = 100
    ) -> list[AuditLogEntry]:
        matches = [
            e for e in self.entries if e.target_type == target_type and e.target_id == target_id
        ]
        matches.sort(key=lambda e: e.occurred_at, reverse=True)
        return matches[:limit]


class FakeIdempotencyRepository(IdempotencyRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str, str], IdempotencyRecord] = {}

    async def get(
        self, customer_id: UUID, route: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        return self._items.get((customer_id, route, idempotency_key))

    async def add(self, record: IdempotencyRecord) -> None:
        self._items[(record.customer_id, record.route, record.idempotency_key)] = record


class FakeNotificationPreferenceRepository(NotificationPreferenceRepository):
    def __init__(self) -> None:
        self._disabled: set[tuple[UUID, NotificationEventType]] = set()

    async def is_enabled(self, linked_identity_id: UUID, event_type: NotificationEventType) -> bool:
        return (linked_identity_id, event_type) not in self._disabled

    async def set_enabled(
        self,
        customer_id: UUID,
        linked_identity_id: UUID,
        event_type: NotificationEventType,
        enabled: bool,
    ) -> None:
        key = (linked_identity_id, event_type)
        if enabled:
            self._disabled.discard(key)
        else:
            self._disabled.add(key)

    async def list_disabled(self, linked_identity_id: UUID) -> set[NotificationEventType]:
        return {t for (i, t) in self._disabled if i == linked_identity_id}


class FakeWebhookEventRepository(WebhookEventRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, WebhookEventRecord] = {}
        self._by_delivery: dict[str, UUID] = {}

    async def get_by_delivery_id(self, delivery_id: str) -> WebhookEventRecord | None:
        record_id = self._by_delivery.get(delivery_id)
        return self._items.get(record_id) if record_id is not None else None

    async def get(self, record_id: UUID) -> WebhookEventRecord | None:
        return self._items.get(record_id)

    async def add(self, record: WebhookEventRecord) -> WebhookEventRecord | None:
        if record.delivery_id in self._by_delivery:
            return None
        self._items[record.id] = record
        self._by_delivery[record.delivery_id] = record.id
        return record

    async def save(self, record: WebhookEventRecord) -> None:
        self._items[record.id] = record


class FakeOutboundMessageRepository(OutboundMessageRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, OutboundMessageRecord] = {}

    async def get_pending(
        self, linked_identity_id: UUID, work_item_id: str
    ) -> OutboundMessageRecord | None:
        for item in self._items.values():
            if (
                item.linked_identity_id == linked_identity_id
                and item.work_item_id == work_item_id
                and item.status == OutboundMessageStatus.PENDING
            ):
                return item
        return None

    async def get(self, record_id: UUID) -> OutboundMessageRecord | None:
        return self._items.get(record_id)

    async def list_created_since(
        self, linked_identity_id: UUID, since: datetime
    ) -> list[OutboundMessageRecord]:
        return sorted(
            (
                r
                for r in self._items.values()
                if r.linked_identity_id == linked_identity_id and r.created_at >= since
            ),
            key=lambda r: r.created_at,
            reverse=True,
        )

    async def add(self, record: OutboundMessageRecord) -> None:
        self._items[record.id] = record

    async def save(self, record: OutboundMessageRecord) -> None:
        self._items[record.id] = record


class FakeAiSettingsRepository(AiSettingsRepository):
    def __init__(self) -> None:
        self._items: dict[UUID, AiSettings] = {}

    async def get_by_customer(self, customer_id: UUID) -> AiSettings | None:
        return self._items.get(customer_id)

    async def save(self, settings: AiSettings) -> None:
        self._items[settings.customer_id] = settings


class FakeClassificationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, ClassificationSettings] = {}

    async def get(self, workspace_connection_id: UUID) -> ClassificationSettings:
        found = self._items.get(workspace_connection_id)
        if found is None:
            return ClassificationSettings(workspace_connection_id=workspace_connection_id)
        return ClassificationSettings(
            workspace_connection_id=found.workspace_connection_id,
            is_enabled=found.is_enabled,
            use_ai_fallback=found.use_ai_fallback,
            rules=list(found.rules),
            updated_at=found.updated_at,
        )

    async def save(self, settings: ClassificationSettings) -> None:
        self._items[settings.workspace_connection_id] = settings


class FakeGroupBindingRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str], GroupBinding] = {}

    async def get_by_group(
        self, channel_installation_id: UUID, group_id: str
    ) -> GroupBinding | None:
        return self._items.get((channel_installation_id, group_id))

    async def list_for_workspace(self, workspace_connection_id: UUID) -> list[GroupBinding]:
        return [
            b for b in self._items.values() if b.workspace_connection_id == workspace_connection_id
        ]

    async def save(self, binding: GroupBinding) -> None:
        self._items[(binding.channel_installation_id, binding.group_id)] = binding

    async def delete(self, binding_id: UUID) -> None:
        self._items = {k: v for k, v in self._items.items() if v.id != binding_id}
