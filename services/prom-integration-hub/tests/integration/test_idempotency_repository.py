"""Integration tests for PostgresIdempotencyRepository against a real Postgres database. Run
with: pytest -m integration (requires
`docker compose -f deployments/docker-compose.yml up -d postgres redis` and `alembic upgrade
head` first — see README).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.adapters.persistence.idempotency_repository import PostgresIdempotencyRepository
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.idempotency.entities import IdempotencyRecord
from src.shared.errors import IdempotencyKeyConflictError

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_customer(session: AsyncSession) -> Customer:
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(session).add(customer)
    return customer


@pytest.mark.asyncio
async def test_idempotency_record_round_trip(db_session: AsyncSession) -> None:
    """PostgresIdempotencyRepository.get() filters `expires_at > datetime.now(UTC)` against the
    real wall clock (it has no injectable Clock port — this table's TTL is enforced at the query
    level, not by application code), so `expires_at` here must be relative to real "now", not the
    fixed `_NOW` used for `created_at`/other fields elsewhere in this file."""
    customer = await _seed_customer(db_session)
    repository = PostgresIdempotencyRepository(db_session)
    record = IdempotencyRecord(
        id=uuid4(),
        customer_id=customer.id,
        route="POST /api/v1/work-items",
        idempotency_key="key-1",
        request_hash="abc123",
        response_status=201,
        response_body='{"id": "wi-1"}',
        created_at=_NOW,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    await repository.add(record)

    fetched = await repository.get(customer.id, "POST /api/v1/work-items", "key-1")
    assert fetched is not None
    assert fetched.request_hash == "abc123"
    assert fetched.response_status == 201


@pytest.mark.asyncio
async def test_expired_record_is_not_returned(db_session: AsyncSession) -> None:
    customer = await _seed_customer(db_session)
    repository = PostgresIdempotencyRepository(db_session)
    record = IdempotencyRecord(
        id=uuid4(),
        customer_id=customer.id,
        route="POST /api/v1/work-items",
        idempotency_key="key-expired",
        request_hash="abc123",
        response_status=201,
        response_body="{}",
        created_at=_NOW - timedelta(hours=48),
        expires_at=_NOW - timedelta(hours=24),
    )
    await repository.add(record)

    fetched = await repository.get(customer.id, "POST /api/v1/work-items", "key-expired")
    assert fetched is None


@pytest.mark.asyncio
async def test_duplicate_key_raises_typed_conflict_error(db_session: AsyncSession) -> None:
    """The unique constraint on (customer_id, route, idempotency_key) is real and reachable (two
    concurrent retries of the same client request), and PostgresIdempotencyRepository.add must
    translate the resulting IntegrityError, same SAVEPOINT pattern as
    PostgresLinkedIdentityRepository.add in Gate 3 — including leaving the transaction usable
    afterward, asserted here the same way (a follow-up Customer insert must still succeed)."""
    customer = await _seed_customer(db_session)
    repository = PostgresIdempotencyRepository(db_session)
    first = IdempotencyRecord(
        id=uuid4(),
        customer_id=customer.id,
        route="POST /api/v1/work-items",
        idempotency_key="key-dup",
        request_hash="abc123",
        response_status=201,
        response_body="{}",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=24),
    )
    await repository.add(first)

    duplicate = IdempotencyRecord(
        id=uuid4(),
        customer_id=customer.id,
        route="POST /api/v1/work-items",
        idempotency_key="key-dup",
        request_hash="different-hash",
        response_status=201,
        response_body="{}",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=24),
    )
    with pytest.raises(IdempotencyKeyConflictError):
        await repository.add(duplicate)

    recovery = Customer(
        id=uuid4(),
        name="Post-Failure",
        status=CustomerStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresCustomerRepository(db_session).add(recovery)
