"""Integration tests for PostgresAiSettingsRepository against real Postgres (migration 0005).
Run with: pytest -m integration tests/integration/test_ai_settings_repository.py (requires
`alembic upgrade head` first — see README)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.ai_settings_repository import PostgresAiSettingsRepository
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.domain.ai.entities import AiSettings
from src.domain.customers.entities import Customer, CustomerStatus

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.mark.asyncio
async def test_save_inserts_then_updates_one_row_per_customer(db_session: AsyncSession) -> None:
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(db_session).add(customer)
    repository = PostgresAiSettingsRepository(db_session)
    assert await repository.get_by_customer(customer.id) is None

    first = AiSettings(
        id=uuid4(),
        customer_id=customer.id,
        is_enabled=True,
        base_url="https://aihub.example/core-api/v1",
        encrypted_api_key=b"encrypted",
        model="gpt-oss-120b",
        timeout_seconds=15,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await repository.save(first)

    later = _NOW + timedelta(minutes=5)
    await repository.save(
        AiSettings(
            id=uuid4(),  # a different id must not create a second row for the same customer
            customer_id=customer.id,
            is_enabled=False,
            base_url="https://other.example/v1",
            encrypted_api_key=None,
            model="Qwen3.8-27B",
            timeout_seconds=30,
            created_at=later,
            updated_at=later,
        )
    )

    stored = await repository.get_by_customer(customer.id)
    assert stored is not None
    assert stored.id == first.id
    assert stored.is_enabled is False
    assert stored.encrypted_api_key is None
    assert stored.model == "Qwen3.8-27B"
    assert stored.created_at == _NOW and stored.updated_at == later
