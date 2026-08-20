"""HTTP-level tests for the workspace-connection routes: GET by id and the webhook-secret admin
endpoint (Gate 7), and POST's get-or-create semantics (Gate 8 — see
RegisterWorkspaceConnection's docstring for why plain create-once semantics stopped being safe).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_register_workspace_connection,
    get_workspace_connection_repository,
)
from apps.api.main import create_app
from src.adapters.clock import SystemClock
from src.application.account_linking.use_cases import RegisterWorkspaceConnection
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.workspaces.entities import WorkspaceConnection
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import FakeAuditLogRepository, FakeWorkspaceConnectionRepository

_SERVICE_TOKEN_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed() -> tuple[WorkspaceConnection, FakeWorkspaceConnectionRepository]:
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="Acme Prom",
        base_url="https://prom.acme.example",
        status=InstanceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
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
    repository = FakeWorkspaceConnectionRepository()
    await repository.add(workspace)
    return workspace, repository


def _build_client(repository: FakeWorkspaceConnectionRepository) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_workspace_connection_repository] = lambda: repository
    app.dependency_overrides[get_register_workspace_connection] = lambda: (
        RegisterWorkspaceConnection(
            workspace_connections=repository,
            audit_log=FakeAuditLogRepository(),
            clock=SystemClock(),
        )
    )
    return TestClient(app)


@pytest.mark.asyncio
async def test_get_before_secret_configured_shows_has_webhook_secret_false() -> None:
    workspace, repository = await _seed()
    client = _build_client(repository)

    response = client.get(
        f"/api/v1/workspace-connections/{workspace.id}", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["has_webhook_secret"] is False
    assert body["webhook_url"].endswith(f"/webhooks/v1/prom/{workspace.id}")


@pytest.mark.asyncio
async def test_get_unknown_id_is_404() -> None:
    _workspace, repository = await _seed()
    client = _build_client(repository)

    response = client.get(
        f"/api/v1/workspace-connections/{uuid4()}", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_set_webhook_secret_never_echoes_raw_value_and_is_reflected_on_get() -> None:
    workspace, repository = await _seed()
    client = _build_client(repository)

    patch_response = client.patch(
        f"/api/v1/workspace-connections/{workspace.id}/webhook-secret",
        json={"secret": "plane-webhook-secret-value"},
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert patch_response.status_code == 200
    body = patch_response.json()
    assert body["has_webhook_secret"] is True
    assert "plane-webhook-secret-value" not in patch_response.text

    get_response = client.get(
        f"/api/v1/workspace-connections/{workspace.id}", headers=_SERVICE_TOKEN_HEADERS
    )
    assert get_response.json()["has_webhook_secret"] is True

    # The repository itself never stores the plaintext — only Fernet ciphertext.
    stored = await repository.get(workspace.id)
    assert stored.webhook_secret_encrypted is not None
    assert b"plane-webhook-secret-value" not in stored.webhook_secret_encrypted


@pytest.mark.asyncio
async def test_set_webhook_secret_unknown_id_is_404() -> None:
    _workspace, repository = await _seed()
    client = _build_client(repository)

    response = client.patch(
        f"/api/v1/workspace-connections/{uuid4()}/webhook-secret",
        json={"secret": "whatever"},
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_is_get_or_create_201_then_200_with_updated_fields() -> None:
    repository = FakeWorkspaceConnectionRepository()
    client = _build_client(repository)
    body = {
        "customer_id": str(uuid4()),
        "prom_instance_id": str(uuid4()),
        "prom_workspace_id": "ws-1",
        "prom_workspace_slug": "acme",
        "name": "Acme Workspace",
    }

    first = client.post("/api/v1/workspace-connections", json=body, headers=_SERVICE_TOKEN_HEADERS)
    assert first.status_code == 201
    first_id = first.json()["id"]

    renamed_body = {**body, "name": "Acme Workspace Renamed", "prom_workspace_slug": "acme-2"}
    second = client.post(
        "/api/v1/workspace-connections", json=renamed_body, headers=_SERVICE_TOKEN_HEADERS
    )
    assert second.status_code == 200
    assert second.json()["id"] == first_id  # same underlying row, not a new one
    assert second.json()["name"] == "Acme Workspace Renamed"
    assert second.json()["prom_workspace_slug"] == "acme-2"


@pytest.mark.asyncio
async def test_missing_service_token_is_rejected() -> None:
    workspace, repository = await _seed()
    client = _build_client(repository)

    response = client.get(f"/api/v1/workspace-connections/{workspace.id}")

    assert response.status_code == 401
