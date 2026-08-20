"""End-to-end (through HTTP, fakes underneath — no real DB) tests for the account-linking
routes: service-token enforcement, the full link flow, and error-code -> status-code mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_consume_linking_code,
    get_create_channel_installation,
    get_customer_repository,
    get_link_installation_to_workspace,
    get_start_account_linking,
    get_unlink_identity,
)
from apps.api.main import create_app
from src.application.account_linking.use_cases import (
    ConsumeLinkingCode,
    CreateChannelInstallation,
    LinkInstallationToWorkspace,
    StartAccountLinking,
    UnlinkIdentity,
)
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelType
from src.domain.workspaces.entities import WorkspaceConnection
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeChannelInstallationRepository,
    FakeCustomerRepository,
    FakeDelegatedCredentialRepository,
    FakeLinkedIdentityRepository,
    FakeLinkingCodeRepository,
    FakePromInstanceRepository,
    FakeWorkspaceConnectionRepository,
)
from tests.fakes.support import (
    FakeChannelCredentialValidator,
    FakeClock,
    FakePromApiClient,
    FakeRateLimiter,
    FakeSecretProvider,
)

_SERVICE_TOKEN_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}


class _Fakes:
    def __init__(self) -> None:
        self.customers = FakeCustomerRepository()
        self.prom_instances = FakePromInstanceRepository()
        self.workspace_connections = FakeWorkspaceConnectionRepository()
        self.installations = FakeChannelInstallationRepository()
        self.linked_identities = FakeLinkedIdentityRepository()
        self.linking_codes = FakeLinkingCodeRepository()
        self.delegated_credentials = FakeDelegatedCredentialRepository()
        self.audit_log = FakeAuditLogRepository()
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.validator = FakeChannelCredentialValidator()
        self.prom_api_client = FakePromApiClient(profile_id_override="prom-user-1")
        self.secrets_provider = FakeSecretProvider()
        self.rate_limiter = FakeRateLimiter()

    async def seed(self) -> tuple[Customer, PromInstance, WorkspaceConnection]:
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
        workspace = WorkspaceConnection(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_workspace_id="ws-1",
            prom_workspace_slug="acme",
            name="Acme Workspace",
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        await self.workspace_connections.add(workspace)
        return customer, instance, workspace


def _build_client(fakes: _Fakes) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_customer_repository] = lambda: fakes.customers
    app.dependency_overrides[get_create_channel_installation] = lambda: CreateChannelInstallation(
        installations=fakes.installations,
        validator=fakes.validator,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    app.dependency_overrides[get_link_installation_to_workspace] = lambda: (
        LinkInstallationToWorkspace(
            installations=fakes.installations,
            workspace_connections=fakes.workspace_connections,
            audit_log=fakes.audit_log,
            clock=fakes.clock,
        )
    )
    app.dependency_overrides[get_start_account_linking] = lambda: StartAccountLinking(
        installations=fakes.installations,
        prom_instances=fakes.prom_instances,
        delegated_credentials=fakes.delegated_credentials,
        linking_codes=fakes.linking_codes,
        prom_api_client=fakes.prom_api_client,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    app.dependency_overrides[get_consume_linking_code] = lambda: ConsumeLinkingCode(
        installations=fakes.installations,
        linking_codes=fakes.linking_codes,
        linked_identities=fakes.linked_identities,
        rate_limiter=fakes.rate_limiter,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    app.dependency_overrides[get_unlink_identity] = lambda: UnlinkIdentity(
        linked_identities=fakes.linked_identities, audit_log=fakes.audit_log, clock=fakes.clock
    )
    return TestClient(app)


def test_missing_service_token_is_rejected() -> None:
    client = _build_client(_Fakes())
    response = client.post("/api/v1/customers", json={"name": "Acme"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_full_link_flow_through_http() -> None:
    fakes = _Fakes()
    customer, instance, workspace = await fakes.seed()
    client = _build_client(fakes)

    create_response = client.post(
        "/api/v1/installations",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": str(customer.id),
            "channel_type": "line",
            "display_name": "Acme LINE Bot",
            "credentials": {"channel_access_token": "token-123"},
        },
    )
    assert create_response.status_code == 201, create_response.text
    installation_id = create_response.json()["id"]

    link_response = client.post(
        f"/api/v1/installations/{installation_id}/workspaces",
        headers=_SERVICE_TOKEN_HEADERS,
        json={"workspace_connection_id": str(workspace.id), "is_default": True},
    )
    assert link_response.status_code == 201, link_response.text

    start_response = client.post(
        "/api/v1/account-linking/start",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "prom_instance_id": str(instance.id),
            "prom_user_id": "prom-user-1",
            "channel_installation_id": installation_id,
            "delegated_token": "delegated-token",
            "default_workspace_connection_id": str(workspace.id),
        },
    )
    assert start_response.status_code == 201, start_response.text
    code = start_response.json()["code"]
    assert len(code) == 6 and code.isdigit()

    consume_response = client.post(
        "/api/v1/account-linking/consume",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "channel_type": "line",
            "channel_user_id": "line-user-1",
            "submitted_code": code,
        },
    )
    assert consume_response.status_code == 201, consume_response.text
    assert consume_response.json()["status"] == "active"
    assert "X-Correlation-Id" in consume_response.headers


@pytest.mark.asyncio
async def test_expired_code_maps_to_422() -> None:
    fakes = _Fakes()
    customer, instance, _workspace = await fakes.seed()
    installation = await CreateChannelInstallation(
        installations=fakes.installations,
        validator=fakes.validator,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    ).execute(
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE Bot",
        credentials={"channel_access_token": "token-123"},
        correlation_id="seed",
    )
    linking_code = await StartAccountLinking(
        installations=fakes.installations,
        prom_instances=fakes.prom_instances,
        delegated_credentials=fakes.delegated_credentials,
        linking_codes=fakes.linking_codes,
        prom_api_client=fakes.prom_api_client,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    ).execute(
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_installation_id=installation.id,
        delegated_token="delegated-token",
        default_workspace_connection_id=None,
        correlation_id="seed",
    )
    fakes.clock.advance(timedelta(minutes=11))
    client = _build_client(fakes)

    response = client.post(
        "/api/v1/account-linking/consume",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": str(installation.id),
            "channel_type": "line",
            "channel_user_id": "line-user-1",
            "submitted_code": linking_code.code,
        },
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "LINKING_CODE_EXPIRED"


@pytest.mark.asyncio
async def test_invalid_code_maps_to_422() -> None:
    fakes = _Fakes()
    customer, _instance, _workspace = await fakes.seed()
    installation = await CreateChannelInstallation(
        installations=fakes.installations,
        validator=fakes.validator,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    ).execute(
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE Bot",
        credentials={"channel_access_token": "token-123"},
        correlation_id="seed",
    )
    client = _build_client(fakes)

    response = client.post(
        "/api/v1/account-linking/consume",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": str(installation.id),
            "channel_type": "line",
            "channel_user_id": "line-user-1",
            "submitted_code": "000000",
        },
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "LINKING_CODE_INVALID"


@pytest.mark.asyncio
async def test_rate_limited_maps_to_429() -> None:
    fakes = _Fakes()
    customer, _instance, _workspace = await fakes.seed()
    installation = await CreateChannelInstallation(
        installations=fakes.installations,
        validator=fakes.validator,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    ).execute(
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE Bot",
        credentials={"channel_access_token": "token-123"},
        correlation_id="seed",
    )
    client = _build_client(fakes)

    request_body = {
        "channel_installation_id": str(installation.id),
        "channel_type": "line",
        "channel_user_id": "line-user-1",
        "submitted_code": "000000",
    }
    for _ in range(5):
        client.post(
            "/api/v1/account-linking/consume", headers=_SERVICE_TOKEN_HEADERS, json=request_body
        )

    response = client.post(
        "/api/v1/account-linking/consume", headers=_SERVICE_TOKEN_HEADERS, json=request_body
    )
    assert response.status_code == 429
    assert response.json()["error_code"] == "RATE_LIMITED"
