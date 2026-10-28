"""PostgresClassificationRepository against real Postgres (migration 0006)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.classification_repository import PostgresClassificationRepository
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.domain.classification.entities import (
    ClassificationRule,
    ClassificationSettings,
    Priority,
)
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.workspaces.entities import WorkspaceConnection

pytestmark = pytest.mark.integration
_NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.mark.asyncio
async def test_defaults_save_and_replace(db_session: AsyncSession) -> None:
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
    repo = PostgresClassificationRepository(db_session)

    default = await repo.get(ws.id)
    assert default.is_enabled and default.use_ai_fallback and default.rules == []
    assert default.updated_at is None

    await repo.save(
        ClassificationSettings(
            ws.id,
            use_ai_fallback=False,
            rules=[
                ClassificationRule("bug", "Bug", Priority.HIGH),
                ClassificationRule("ด่วน", None, Priority.URGENT),
            ],
            updated_at=_NOW,
        )
    )
    await repo.save(
        ClassificationSettings(ws.id, rules=[ClassificationRule("ui", "UI", None)], updated_at=_NOW)
    )
    stored = await repo.get(ws.id)
    assert stored.use_ai_fallback is True
    assert [(r.keyword, r.label_name, r.priority) for r in stored.rules] == [("ui", "UI", None)]
