"""HTTP-level tests for Gate 8's installation-admin routes: GET detail, DELETE workspace-unlink,
POST test-connection, POST activate/deactivate. Service-token enforcement and the full
create/link/revoke flow are already covered by tests/api/test_account_linking_routes.py — this
file only proves the new routes' own plumbing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_activate_installation,
    get_channel_installation_repository,
    get_deactivate_installation,
    get_delete_channel_installation,
    get_test_channel_connection,
    get_unlink_workspace_from_installation,
)
from apps.api.main import create_app
from src.application.account_linking.use_cases import (
    ActivateInstallation,
    DeactivateInstallation,
    DeleteChannelInstallation,
    TestChannelConnection,
    UnlinkWorkspaceFromInstallation,
)
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.integrations.entities import (
    ChannelInstallation,
    ChannelType,
    InstallationStatus,
    InstallationWorkspace,
)
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import (
    FakeChannelInstallationRepository,
    FakeLinkedIdentityRepository,
)
from tests.fakes.support import FakeChannelCredentialValidator, FakeClock, FakeSecretProvider

_SERVICE_TOKEN_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _Fakes:
    def __init__(self) -> None:
        self.installations = FakeChannelInstallationRepository()
        self.linked_identities = FakeLinkedIdentityRepository()
        self.audit_log = _AuditLog()
        self.clock = FakeClock(_NOW)
        self.validator = FakeChannelCredentialValidator()
        self.secrets_provider = FakeSecretProvider()

    async def seed_installation(
        self, status: InstallationStatus = InstallationStatus.ACTIVE
    ) -> ChannelInstallation:
        installation = ChannelInstallation(
            id=uuid4(),
            customer_id=uuid4(),
            channel_type=ChannelType.LINE,
            display_name="Acme LINE Bot",
            status=status,
            external_account_id="external-1",
            encrypted_credentials=self.secrets_provider.encrypt(b'{"channel_access_token":"t"}'),
            default_workspace_connection_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
        await self.installations.add(installation)
        return installation


class _AuditLog:
    def __init__(self) -> None:
        self.entries: list = []

    async def add(self, entry: object) -> None:
        self.entries.append(entry)


def _build_client(fakes: _Fakes) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_channel_installation_repository] = lambda: fakes.installations
    app.dependency_overrides[get_unlink_workspace_from_installation] = lambda: (
        UnlinkWorkspaceFromInstallation(
            installations=fakes.installations, audit_log=fakes.audit_log, clock=fakes.clock
        )
    )
    app.dependency_overrides[get_test_channel_connection] = lambda: TestChannelConnection(
        installations=fakes.installations,
        validator=fakes.validator,
        secrets_provider=fakes.secrets_provider,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    app.dependency_overrides[get_activate_installation] = lambda: ActivateInstallation(
        installations=fakes.installations, audit_log=fakes.audit_log, clock=fakes.clock
    )
    app.dependency_overrides[get_deactivate_installation] = lambda: DeactivateInstallation(
        installations=fakes.installations, audit_log=fakes.audit_log, clock=fakes.clock
    )
    app.dependency_overrides[get_delete_channel_installation] = lambda: DeleteChannelInstallation(
        installations=fakes.installations,
        linked_identities=fakes.linked_identities,
        audit_log=fakes.audit_log,
        clock=fakes.clock,
    )
    return TestClient(app)


@pytest.mark.asyncio
async def test_get_installation_detail_includes_workspaces() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation()
    workspace_connection_id = uuid4()

    await fakes.installations.add_workspace_link(
        InstallationWorkspace(
            id=uuid4(),
            channel_installation_id=installation.id,
            workspace_connection_id=workspace_connection_id,
            created_at=_NOW,
        )
    )
    client = _build_client(fakes)

    response = client.get(
        f"/api/v1/installations/{installation.id}", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(installation.id)
    assert len(body["workspaces"]) == 1
    assert body["workspaces"][0]["workspace_connection_id"] == str(workspace_connection_id)
    assert "encrypted_credentials" not in body


def test_get_installation_detail_unknown_id_is_404() -> None:
    client = _build_client(_Fakes())
    response = client.get(f"/api/v1/installations/{uuid4()}", headers=_SERVICE_TOKEN_HEADERS)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unlink_workspace_route() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation()
    workspace_connection_id = uuid4()

    await fakes.installations.add_workspace_link(
        InstallationWorkspace(
            id=uuid4(),
            channel_installation_id=installation.id,
            workspace_connection_id=workspace_connection_id,
            created_at=_NOW,
        )
    )
    client = _build_client(fakes)

    response = client.delete(
        f"/api/v1/installations/{installation.id}/workspaces/{workspace_connection_id}",
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert response.status_code == 204
    assert (
        await fakes.installations.get_workspace_link(installation.id, workspace_connection_id)
        is None
    )


@pytest.mark.asyncio
async def test_unlink_workspace_route_unknown_link_is_404() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation()
    client = _build_client(fakes)

    response = client.delete(
        f"/api/v1/installations/{installation.id}/workspaces/{uuid4()}",
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_test_connection_route_reports_validity_without_raising() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation()
    fakes.validator.is_valid = False
    fakes.validator.error_message = "bad token"
    client = _build_client(fakes)

    response = client.post(
        f"/api/v1/installations/{installation.id}/test-connection",
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_valid"] is False
    assert body["error_message"] == "bad token"


@pytest.mark.asyncio
async def test_activate_and_deactivate_routes() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation()
    client = _build_client(fakes)

    deactivate_response = client.post(
        f"/api/v1/installations/{installation.id}/deactivate", headers=_SERVICE_TOKEN_HEADERS
    )
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["status"] == "inactive"

    activate_response = client.post(
        f"/api/v1/installations/{installation.id}/activate", headers=_SERVICE_TOKEN_HEADERS
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["status"] == "active"


@pytest.mark.asyncio
async def test_activate_a_revoked_installation_is_409() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation(status=InstallationStatus.REVOKED)
    client = _build_client(fakes)

    response = client.post(
        f"/api/v1/installations/{installation.id}/activate", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "INSTALLATION_REVOKED"


def test_missing_service_token_is_rejected() -> None:
    client = _build_client(_Fakes())
    response = client.get(f"/api/v1/installations/{uuid4()}")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_delete_a_revoked_unused_installation_route() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation(status=InstallationStatus.REVOKED)
    client = _build_client(fakes)

    response = client.delete(
        f"/api/v1/installations/{installation.id}", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 204
    assert await fakes.installations.get(installation.id) is None


@pytest.mark.asyncio
async def test_delete_an_active_installation_route_is_409() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation(status=InstallationStatus.ACTIVE)
    client = _build_client(fakes)

    response = client.delete(
        f"/api/v1/installations/{installation.id}", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "INSTALLATION_NOT_REVOKED"


@pytest.mark.asyncio
async def test_delete_a_revoked_installation_with_linked_identity_route_is_409() -> None:
    fakes = _Fakes()
    installation = await fakes.seed_installation(status=InstallationStatus.REVOKED)
    await fakes.linked_identities.add(
        LinkedIdentity(
            id=uuid4(),
            customer_id=installation.customer_id,
            channel_installation_id=installation.id,
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-1",
            prom_instance_id=uuid4(),
            prom_user_id="prom-user-1",
            default_workspace_connection_id=None,
            status=IdentityStatus.UNLINKED,
            linked_at=_NOW,
            unlinked_at=_NOW,
        )
    )
    client = _build_client(fakes)

    response = client.delete(
        f"/api/v1/installations/{installation.id}", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "INSTALLATION_HAS_LINKED_IDENTITIES"


def test_delete_installation_route_unknown_id_is_404() -> None:
    client = _build_client(_Fakes())
    response = client.delete(f"/api/v1/installations/{uuid4()}", headers=_SERVICE_TOKEN_HEADERS)
    assert response.status_code == 404
