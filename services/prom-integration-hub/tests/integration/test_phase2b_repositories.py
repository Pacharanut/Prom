"""Phase 2B repository additions against real Postgres: preference list_disabled + upsert,
outbound list_created_since, linked identity list_all_active. Run with
`pytest -m integration tests/integration/test_phase2b_repositories.py`."""

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
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationStatus
from src.domain.notifications.entities import (
    NotificationEventType,
    OutboundMessageRecord,
    OutboundMessageStatus,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 26, tzinfo=UTC)


async def _identity(session: AsyncSession) -> LinkedIdentity:
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(session).add(customer)
    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="P",
        base_url="https://prom.example.com",
        status=InstanceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresPromInstanceRepository(session).add(instance)
    installation = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="L",
        status=InstallationStatus.ACTIVE,
        external_account_id=f"@{uuid4().hex[:8]}",
        encrypted_credentials=b"x",
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
        channel_user_id=f"U{uuid4().hex[:8]}",
        prom_instance_id=instance.id,
        prom_user_id=f"u{uuid4().hex[:8]}",
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=_NOW,
        unlinked_at=None,
    )
    await PostgresLinkedIdentityRepository(session).add(identity)
    return identity


@pytest.mark.asyncio
async def test_preferences_upsert_and_list_disabled(db_session: AsyncSession) -> None:
    identity = await _identity(db_session)
    repo = PostgresNotificationPreferenceRepository(db_session)
    assert await repo.list_disabled(identity.id) == set()

    await repo.set_enabled(identity.customer_id, identity.id, NotificationEventType.OVERDUE, False)
    await repo.set_enabled(identity.customer_id, identity.id, NotificationEventType.COMMENT, False)
    await repo.set_enabled(identity.customer_id, identity.id, NotificationEventType.COMMENT, True)

    assert await repo.list_disabled(identity.id) == {NotificationEventType.OVERDUE}
    assert await repo.is_enabled(identity.id, NotificationEventType.COMMENT) is True


@pytest.mark.asyncio
async def test_outbound_list_created_since_and_active_identities(db_session: AsyncSession) -> None:
    identity = await _identity(db_session)
    repo = PostgresOutboundMessageRepository(db_session)
    for hours_ago, work_item_id in ((2, "recent"), (30, "old")):
        await repo.add(
            OutboundMessageRecord(
                id=uuid4(),
                linked_identity_id=identity.id,
                work_item_id=work_item_id,
                event_types_batched=[NotificationEventType.ASSIGNED],
                status=OutboundMessageStatus.SENT,
                attempt_count=1,
                scheduled_for=_NOW,
                sent_at=_NOW,
                created_at=_NOW - timedelta(hours=hours_ago),
            )
        )
    recent = await repo.list_created_since(identity.id, _NOW - timedelta(hours=24))
    assert [r.work_item_id for r in recent] == ["recent"]

    active = await PostgresLinkedIdentityRepository(db_session).list_all_active()
    assert identity.id in {i.id for i in active}
