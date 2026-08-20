"""End-to-end (through HTTP, fakes underneath — no real DB) tests for the work-item routes:
the happy-path create flow, and the Idempotency-Key cache behavior (repeat -> cached response
without re-invoking the use case; reuse with a different body -> 409 conflict).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_channel_installation_repository,
    get_create_channel_installation,
    get_create_work_item,
    get_customer_repository,
    get_idempotency_repository,
    get_prom_instance_repository,
)
from apps.api.main import create_app
from src.application.account_linking.use_cases import CreateChannelInstallation
from src.application.work_items.use_cases import CreateWorkItem
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import DelegatedCredential
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelType
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeChannelInstallationRepository,
    FakeCustomerRepository,
    FakeDelegatedCredentialRepository,
    FakeIdempotencyRepository,
    FakePromInstanceRepository,
)
from tests.fakes.support import (
    FakeChannelCredentialValidator,
    FakeClock,
    FakePromApiClient,
    FakeSecretProvider,
)

_SERVICE_TOKEN_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_PROM_USER_ID = "prom-user-1"


class _Fakes:
    def __init__(self) -> None:
        self.customers = FakeCustomerRepository()
        self.prom_instances = FakePromInstanceRepository()
        self.installations = FakeChannelInstallationRepository()
        self.delegated_credentials = FakeDelegatedCredentialRepository()
        self.audit_log = FakeAuditLogRepository()
        self.idempotency = FakeIdempotencyRepository()
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.validator = FakeChannelCredentialValidator()
        self.secrets_provider = FakeSecretProvider()
        self.prom_api_client = FakePromApiClient()

    async def seed(self) -> tuple[Customer, PromInstance, str]:
        customer = Customer(
            id=uuid4(),
            name="Acme",
            status=CustomerStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.customers.add(customer)

        instance = PromInstance(
            id=uuid4(),
            customer_id=customer.id,
            name="Acme Prom",
            base_url="https://prom.acme.example",
            status=InstanceStatus.ACTIVE,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.prom_instances.add(instance)

        installation = await CreateChannelInstallation(
            installations=self.installations,
            validator=self.validator,
            secrets_provider=self.secrets_provider,
            audit_log=self.audit_log,
            clock=self.clock,
        ).execute(
            customer_id=customer.id,
            channel_type=ChannelType.LINE,
            display_name="Acme LINE Bot",
            credentials={"channel_access_token": "token-123"},
            correlation_id="seed",
        )

        credential = DelegatedCredential(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_user_id=_PROM_USER_ID,
            encrypted_token=self.secrets_provider.encrypt(b"delegated-token"),
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            rotated_at=None,
        )
        await self.delegated_credentials.upsert(credential)

        return customer, instance, str(installation.id)


def _build_client(fakes: _Fakes) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_customer_repository] = lambda: fakes.customers
    app.dependency_overrides[get_prom_instance_repository] = lambda: fakes.prom_instances
    app.dependency_overrides[get_channel_installation_repository] = lambda: fakes.installations
    app.dependency_overrides[get_idempotency_repository] = lambda: fakes.idempotency
    app.dependency_overrides[get_create_channel_installation] = lambda: CreateChannelInstallation(
        installations=fakes.installations,
        validator=fakes.validator,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    app.dependency_overrides[get_create_work_item] = lambda: CreateWorkItem(
        prom_instances=fakes.prom_instances,
        delegated_credentials=fakes.delegated_credentials,
        secrets_provider=fakes.secrets_provider,
        prom_api_client=fakes.prom_api_client,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    return TestClient(app)


def _create_body(
    customer: Customer, instance: PromInstance, installation_id: str
) -> dict[str, object]:
    return {
        "channel_installation_id": installation_id,
        "prom_instance_id": str(instance.id),
        "prom_user_id": _PROM_USER_ID,
        "workspace_slug": "acme",
        "project_id": "proj-1",
        "title": "Fix the bug",
    }


def test_missing_service_token_is_rejected() -> None:
    client = _build_client(_Fakes())
    response = client.post("/api/v1/work-items", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_work_item_happy_path() -> None:
    fakes = _Fakes()
    customer, instance, installation_id = await fakes.seed()
    client = _build_client(fakes)

    response = client.post(
        "/api/v1/work-items",
        headers=_SERVICE_TOKEN_HEADERS,
        json=_create_body(customer, instance, installation_id),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["work_item"]["name"] == "Fix the bug"
    assert body["was_replay"] is False
    assert "X-Correlation-Id" in response.headers


@pytest.mark.asyncio
async def test_idempotency_key_repeat_returns_cached_response_without_reinvoking_use_case() -> None:
    fakes = _Fakes()
    customer, instance, installation_id = await fakes.seed()
    client = _build_client(fakes)
    body = _create_body(customer, instance, installation_id)
    body["idempotency_key"] = "client-retry-key"
    headers = {**_SERVICE_TOKEN_HEADERS, "Idempotency-Key": "route-level-key-1"}

    first = client.post("/api/v1/work-items", headers=headers, json=body)
    assert first.status_code == 201, first.text
    calls_after_first = len(fakes.prom_api_client.work_items)

    second = client.post("/api/v1/work-items", headers=headers, json=body)
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    # No new work item was created on the replayed request — the cached response short-circuited
    # before the use case (and therefore the Prom API call) ran again.
    assert len(fakes.prom_api_client.work_items) == calls_after_first


@pytest.mark.asyncio
async def test_idempotency_key_reused_with_different_body_conflicts() -> None:
    fakes = _Fakes()
    customer, instance, installation_id = await fakes.seed()
    client = _build_client(fakes)
    headers = {**_SERVICE_TOKEN_HEADERS, "Idempotency-Key": "route-level-key-2"}

    body_a = _create_body(customer, instance, installation_id)
    body_a["title"] = "First title"
    first = client.post("/api/v1/work-items", headers=headers, json=body_a)
    assert first.status_code == 201, first.text

    body_b = _create_body(customer, instance, installation_id)
    body_b["title"] = "Different title"
    second = client.post("/api/v1/work-items", headers=headers, json=body_b)
    assert second.status_code == 409
    assert second.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"
