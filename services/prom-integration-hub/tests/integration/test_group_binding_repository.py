"""PostgresGroupBindingRepository against real Postgres (migration 0007)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.adapters.persistence.group_binding_repository import PostgresGroupBindingRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.groups.entities import GroupBinding, GroupEventType
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationStatus
from src.domain.workspaces.entities import WorkspaceConnection

pytestmark = pytest.mark.integration
_NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.mark.asyncio
async def test_upsert_list_delete(db_session: AsyncSession) -> None:
    customer = Customer(
        id=uuid4(), name="A", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(db_session).add(customer)
    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="P",
        base_url="https://prom.example.com",
        status=InstanceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresPromInstanceRepository(db_session).add(instance)
    ws = WorkspaceConnection(
        id=uuid4(),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_workspace_id=f"w-{uuid4().hex[:6]}",
        prom_workspace_slug="a",
        name="A",
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresWorkspaceConnectionRepository(db_session).add(ws)
    installation = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="L",
        status=InstallationStatus.ACTIVE,
        external_account_id=f"@{uuid4().hex[:6]}",
        encrypted_credentials=b"x",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresChannelInstallationRepository(db_session).add(installation)
    repo = PostgresGroupBindingRepository(db_session)

    binding = GroupBinding(
        id=uuid4(),
        customer_id=customer.id,
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        group_id="C123",
        workspace_connection_id=ws.id,
        project_id="p1",
        project_name="AI",
        bound_by_prom_user_id="u1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    await repo.save(binding)
    binding.events = {GroupEventType.CREATED, GroupEventType.COMPLETED}
    binding.project_id = None
    await repo.save(binding)

    stored = await repo.get_by_group(installation.id, "C123")
    assert stored is not None and stored.project_id is None
    assert stored.events == {GroupEventType.CREATED, GroupEventType.COMPLETED}
    assert [b.group_id for b in await repo.list_for_workspace(ws.id)] == ["C123"]

    await repo.delete(stored.id)
    assert await repo.get_by_group(installation.id, "C123") is None
