"""Postgres-backed CustomerRepository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.models import CustomerModel
from src.application.account_linking.ports import CustomerRepository
from src.domain.customers.entities import Customer, CustomerStatus


def _to_domain(model: CustomerModel) -> Customer:
    return Customer(
        id=model.id,
        name=model.name,
        status=CustomerStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class PostgresCustomerRepository(CustomerRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, customer_id: UUID) -> Customer | None:
        model = await self._session.get(CustomerModel, customer_id)
        return _to_domain(model) if model is not None else None

    async def add(self, customer: Customer) -> None:
        self._session.add(
            CustomerModel(
                id=customer.id,
                name=customer.name,
                status=customer.status.value,
                created_at=customer.created_at,
                updated_at=customer.updated_at,
            )
        )
        await self._session.flush()
