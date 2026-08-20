"""HTTP-level tests for Gate 8's three read-only admin listing routes:
GET /api/v1/customers/{id}/installations, GET /api/v1/customers/{id}/audit-log,
GET /api/v1/prom-instances/{id}/prom-users/{prom_user_id}/linked-identities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_audit_log_repository,
    get_channel_installation_repository,
    get_linked_identity_repository,
)
from apps.api.main import create_app
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationStatus
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import FakeAuditLogRepository, FakeChannelInstallationRepository
from tests.fakes.support import FakeSecretProvider

_SERVICE_TOKEN_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _build_client(installations=None, audit_log=None, linked_identities=None) -> TestClient:  # noqa: E501
    app = create_app()
    if installations is not None:
        app.dependency_overrides[get_channel_installation_repository] = lambda: installations
    if audit_log is not None:
        app.dependency_overrides[get_audit_log_repository] = lambda: audit_log
    if linked_identities is not None:
        app.dependency_overrides[get_linked_identity_repository] = lambda: linked_identities
    return TestClient(app)


async def test_list_customer_installations() -> None:
    customer_id = uuid4()
    installations = FakeChannelInstallationRepository()
    secrets_provider = FakeSecretProvider()
    for status in (InstallationStatus.ACTIVE, InstallationStatus.INACTIVE):
        await installations.add(
            ChannelInstallation(
                id=uuid4(),
                customer_id=customer_id,
                channel_type=ChannelType.LINE,
                display_name=f"Bot ({status.value})",
                status=status,
                external_account_id="external-1",
                encrypted_credentials=secrets_provider.encrypt(b"{}"),
                default_workspace_connection_id=None,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    # A different customer's installation must not leak into the list.
    await installations.add(
        ChannelInstallation(
            id=uuid4(),
            customer_id=uuid4(),
            channel_type=ChannelType.DISCORD,
            display_name="Other customer's bot",
            status=InstallationStatus.ACTIVE,
            external_account_id="external-2",
            encrypted_credentials=secrets_provider.encrypt(b"{}"),
            default_workspace_connection_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    client = _build_client(installations=installations)

    response = client.get(
        f"/api/v1/customers/{customer_id}/installations", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 2
    assert {item["status"] for item in body} == {"active", "inactive"}
    assert all("encrypted_credentials" not in item for item in body)


async def test_list_customer_audit_log_most_recent_first_and_filterable() -> None:
    customer_id = uuid4()
    installation_id = uuid4()
    audit_log = FakeAuditLogRepository()
    older = AuditLogEntry(
        id=uuid4(),
        customer_id=customer_id,
        prom_instance_id=None,
        installation_id=installation_id,
        channel_type=None,
        channel_user_id=None,
        prom_user_id=None,
        action="installation.activated",
        target_type="channel_installation",
        target_id=str(installation_id),
        correlation_id="corr-1",
        result_status=ResultStatus.SUCCESS,
        error_code=None,
        occurred_at=_NOW,
    )
    newer = AuditLogEntry(
        id=uuid4(),
        customer_id=customer_id,
        prom_instance_id=None,
        installation_id=installation_id,
        channel_type=None,
        channel_user_id=None,
        prom_user_id=None,
        action="installation.disabled",
        target_type="channel_installation",
        target_id=str(installation_id),
        correlation_id="corr-2",
        result_status=ResultStatus.SUCCESS,
        error_code=None,
        occurred_at=_NOW + timedelta(minutes=5),
    )
    unrelated_customer_entry = AuditLogEntry(
        id=uuid4(),
        customer_id=uuid4(),
        prom_instance_id=None,
        installation_id=None,
        channel_type=None,
        channel_user_id=None,
        prom_user_id=None,
        action="installation.activated",
        target_type="channel_installation",
        target_id=None,
        correlation_id="corr-3",
        result_status=ResultStatus.SUCCESS,
        error_code=None,
        occurred_at=_NOW,
    )
    await audit_log.add(older)
    await audit_log.add(newer)
    await audit_log.add(unrelated_customer_entry)
    client = _build_client(audit_log=audit_log)

    response = client.get(
        f"/api/v1/customers/{customer_id}/audit-log", headers=_SERVICE_TOKEN_HEADERS
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 2  # unrelated customer's entry excluded
    assert body[0]["action"] == "installation.disabled"  # most recent first
    assert body[1]["action"] == "installation.activated"


async def test_list_customer_audit_log_installation_filter() -> None:
    customer_id = uuid4()
    matching_installation_id = uuid4()
    audit_log = FakeAuditLogRepository()
    await audit_log.add(
        AuditLogEntry(
            id=uuid4(),
            customer_id=customer_id,
            prom_instance_id=None,
            installation_id=matching_installation_id,
            channel_type=None,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.activated",
            target_type="channel_installation",
            target_id=None,
            correlation_id="corr-1",
            result_status=ResultStatus.SUCCESS,
            error_code=None,
            occurred_at=_NOW,
        )
    )
    await audit_log.add(
        AuditLogEntry(
            id=uuid4(),
            customer_id=customer_id,
            prom_instance_id=None,
            installation_id=uuid4(),  # a different installation, same customer
            channel_type=None,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.activated",
            target_type="channel_installation",
            target_id=None,
            correlation_id="corr-2",
            result_status=ResultStatus.SUCCESS,
            error_code=None,
            occurred_at=_NOW,
        )
    )
    client = _build_client(audit_log=audit_log)

    response = client.get(
        f"/api/v1/customers/{customer_id}/audit-log",
        params={"installation_id": str(matching_installation_id)},
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["installation_id"] == str(matching_installation_id)


async def test_list_linked_identities_for_prom_user() -> None:
    prom_instance_id = uuid4()
    prom_user_id = "prom-user-1"
    linked_identities = _FakeLinkedIdentities()
    await linked_identities.add(
        LinkedIdentity(
            id=uuid4(),
            customer_id=uuid4(),
            channel_installation_id=uuid4(),
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-1",
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            default_workspace_connection_id=None,
            status=IdentityStatus.ACTIVE,
            linked_at=_NOW,
            unlinked_at=None,
        )
    )
    await linked_identities.add(
        LinkedIdentity(
            id=uuid4(),
            customer_id=uuid4(),
            channel_installation_id=uuid4(),
            channel_type=ChannelType.DISCORD,
            channel_user_id="discord-user-1",
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            default_workspace_connection_id=None,
            status=IdentityStatus.ACTIVE,
            linked_at=_NOW,
            unlinked_at=None,
        )
    )
    # A different Prom user must not leak into the list.
    await linked_identities.add(
        LinkedIdentity(
            id=uuid4(),
            customer_id=uuid4(),
            channel_installation_id=uuid4(),
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-2",
            prom_instance_id=prom_instance_id,
            prom_user_id="prom-user-2",
            default_workspace_connection_id=None,
            status=IdentityStatus.ACTIVE,
            linked_at=_NOW,
            unlinked_at=None,
        )
    )
    client = _build_client(linked_identities=linked_identities)

    response = client.get(
        f"/api/v1/prom-instances/{prom_instance_id}/prom-users/{prom_user_id}/linked-identities",
        headers=_SERVICE_TOKEN_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 2
    assert {item["channel_type"] for item in body} == {"line", "discord"}


class _FakeLinkedIdentities:
    """Minimal in-memory double — reuses the real filtering semantics of
    `list_active_by_prom_user` without pulling in the full account-linking fake's unrelated
    surface (get_active_by_channel_user's uniqueness enforcement etc.), which this test doesn't
    exercise."""

    def __init__(self) -> None:
        self._items: dict = {}

    async def add(self, identity: LinkedIdentity) -> None:
        self._items[identity.id] = identity

    async def list_active_by_prom_user(
        self,
        prom_instance_id,
        prom_user_id,  # noqa: ANN001
    ) -> list[LinkedIdentity]:
        return [
            item
            for item in self._items.values()
            if item.prom_instance_id == prom_instance_id
            and item.prom_user_id == prom_user_id
            and item.status == IdentityStatus.ACTIVE
        ]


def test_missing_service_token_is_rejected() -> None:
    client = _build_client()
    response = client.get(f"/api/v1/customers/{uuid4()}/installations")
    assert response.status_code == 401
