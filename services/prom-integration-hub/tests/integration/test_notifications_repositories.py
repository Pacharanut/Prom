"""Integration tests for Gate 7's three new repositories against a real Postgres database. Run
with: pytest -m integration (requires
`docker compose -f deployments/docker-compose.yml up -d postgres redis` and `alembic upgrade
head` first — see README).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.notification_preference_repository import (
    PostgresNotificationPreferenceRepository,
)
from src.adapters.persistence.outbound_message_repository import PostgresOutboundMessageRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.webhook_event_repository import PostgresWebhookEventRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationStatus
from src.domain.notifications.entities import (
    NotificationEventType,
    OutboundMessageRecord,
    OutboundMessageStatus,
)
from src.domain.webhooks.entities import WebhookEventRecord, WebhookEventStatus
from src.domain.workspaces.entities import WorkspaceConnection

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_linked_identity(
    session: AsyncSession,
) -> tuple[Customer, WorkspaceConnection, LinkedIdentity]:
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(session).add(customer)

    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="Acme Prom",
        base_url="https://prom.acme.example",
        status=InstanceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresPromInstanceRepository(session).add(instance)

    workspace = WorkspaceConnection(
        id=uuid4(),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_workspace_id="ws-1",
        prom_workspace_slug="acme",
        name="Acme Workspace",
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresWorkspaceConnectionRepository(session).add(workspace)

    installation = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE OA",
        status=InstallationStatus.ACTIVE,
        external_account_id="line-account-1",
        encrypted_credentials=b"ciphertext",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresChannelInstallationRepository(session).add(installation)

    identity = LinkedIdentity(
        id=uuid4(),
        customer_id=customer.id,
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-1",
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=_NOW,
        unlinked_at=None,
    )
    await PostgresLinkedIdentityRepository(session).add(identity)
    return customer, workspace, identity


# --- WebhookEventRepository ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_event_round_trip_and_delivery_lookup(db_session: AsyncSession) -> None:
    _customer, workspace, _identity = await _seed_linked_identity(db_session)
    repository = PostgresWebhookEventRepository(db_session)
    record = WebhookEventRecord(
        id=uuid4(),
        workspace_connection_id=workspace.id,
        delivery_id="delivery-1",
        event="issue",
        action="create",
        received_at=_NOW,
        processed_at=None,
        status=WebhookEventStatus.RECEIVED,
    )

    stored = await repository.add(record)
    assert stored is not None

    fetched = await repository.get_by_delivery_id("delivery-1")
    assert fetched is not None
    assert fetched.event == "issue"
    assert fetched.status is WebhookEventStatus.RECEIVED

    fetched.status = WebhookEventStatus.PROCESSED
    fetched.processed_at = _NOW
    await repository.save(fetched)
    resaved = await repository.get(fetched.id)
    assert resaved.status is WebhookEventStatus.PROCESSED
    assert resaved.processed_at is not None


@pytest.mark.asyncio
async def test_duplicate_delivery_id_add_returns_none_not_a_raw_integrity_error(
    db_session: AsyncSession,
) -> None:
    """A real, reachable race: Plane retries a delivery before the first attempt's row is
    committed. PostgresWebhookEventRepository.add SAVEPOINT-wraps the insert and returns None on
    a duplicate `delivery_id` rather than raising (see its docstring — this is the expected shape
    of "already recorded", not a caller error) — and the surrounding transaction must remain
    usable afterward, asserted the same way as Gate 3's SAVEPOINT tests: a follow-up insert still
    succeeds in the same session.
    """
    _customer, workspace, _identity = await _seed_linked_identity(db_session)
    repository = PostgresWebhookEventRepository(db_session)
    first = WebhookEventRecord(
        id=uuid4(),
        workspace_connection_id=workspace.id,
        delivery_id="delivery-dup",
        event="issue",
        action="create",
        received_at=_NOW,
        processed_at=None,
        status=WebhookEventStatus.RECEIVED,
    )
    assert await repository.add(first) is not None

    duplicate = WebhookEventRecord(
        id=uuid4(),
        workspace_connection_id=workspace.id,
        delivery_id="delivery-dup",
        event="issue",
        action="update",
        received_at=_NOW,
        processed_at=None,
        status=WebhookEventStatus.RECEIVED,
    )
    assert await repository.add(duplicate) is None

    recovery_customer = Customer(
        id=uuid4(),
        name="Post-Failure",
        status=CustomerStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresCustomerRepository(db_session).add(recovery_customer)


# --- NotificationPreferenceRepository ---------------------------------------------------------


@pytest.mark.asyncio
async def test_absence_of_row_means_enabled(db_session: AsyncSession) -> None:
    _customer, _workspace, identity = await _seed_linked_identity(db_session)
    repository = PostgresNotificationPreferenceRepository(db_session)

    assert await repository.is_enabled(identity.id, NotificationEventType.ASSIGNED) is True


@pytest.mark.asyncio
async def test_set_enabled_false_then_true_round_trips(db_session: AsyncSession) -> None:
    customer, _workspace, identity = await _seed_linked_identity(db_session)
    repository = PostgresNotificationPreferenceRepository(db_session)

    await repository.set_enabled(
        customer.id, identity.id, NotificationEventType.COMMENT, enabled=False
    )
    assert await repository.is_enabled(identity.id, NotificationEventType.COMMENT) is False
    # Other event types for the same identity are unaffected.
    assert await repository.is_enabled(identity.id, NotificationEventType.ASSIGNED) is True

    # Upsert semantics: a second call for the same (identity, event_type) updates in place
    # rather than violating the unique constraint on (linked_identity_id, event_type).
    await repository.set_enabled(
        customer.id, identity.id, NotificationEventType.COMMENT, enabled=True
    )
    assert await repository.is_enabled(identity.id, NotificationEventType.COMMENT) is True


# --- OutboundMessageRepository -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbound_message_round_trip_and_pending_lookup(db_session: AsyncSession) -> None:
    _customer, _workspace, identity = await _seed_linked_identity(db_session)
    repository = PostgresOutboundMessageRepository(db_session)
    record = OutboundMessageRecord(
        id=uuid4(),
        linked_identity_id=identity.id,
        work_item_id="wi-1",
        event_types_batched=[NotificationEventType.ASSIGNED],
        status=OutboundMessageStatus.PENDING,
        attempt_count=0,
        scheduled_for=_NOW + timedelta(seconds=45),
        sent_at=None,
        created_at=_NOW,
    )
    await repository.add(record)

    pending = await repository.get_pending(identity.id, "wi-1")
    assert pending is not None
    assert pending.event_types_batched == [NotificationEventType.ASSIGNED]

    pending.event_types_batched.append(NotificationEventType.STATE_CHANGED)
    pending.status = OutboundMessageStatus.SENT
    pending.sent_at = _NOW
    await repository.save(pending)

    # Once status is no longer PENDING, get_pending must not return it — a fresh debounce record
    # for the same (identity, work_item) pair is expected to follow (see
    # test_event_after_a_completed_send_gets_a_fresh_record in test_notifications.py).
    assert await repository.get_pending(identity.id, "wi-1") is None

    resaved = await repository.get(record.id)
    assert resaved.status is OutboundMessageStatus.SENT
    assert set(resaved.event_types_batched) == {
        NotificationEventType.ASSIGNED,
        NotificationEventType.STATE_CHANGED,
    }
