"""Unit tests for Gate 8's installation-admin use cases, using in-memory fakes only (no infra):
UnlinkWorkspaceFromInstallation, TestChannelConnection, ActivateInstallation,
DeactivateInstallation.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from src.domain.integrations.entities import InstallationStatus
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import (
    ChannelCredentialInvalidError,
    InstallationRevokedError,
    IntegrationInactiveError,
    NotFoundError,
)
from tests.application.conftest import AccountLinkingHarness

# --- UnlinkWorkspaceFromInstallation --------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_workspace_removes_link(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    installation = await harness.create_active_installation(customer.id)
    workspace = await _seed_workspace(harness, customer.id, instance.id)
    await harness.link_workspace.execute(
        channel_installation_id=installation.id,
        workspace_connection_id=workspace.id,
        is_default=True,
        correlation_id="corr-1",
    )

    await harness.unlink_workspace.execute(
        channel_installation_id=installation.id,
        workspace_connection_id=workspace.id,
        correlation_id="corr-2",
    )

    assert await harness.installations.get_workspace_link(installation.id, workspace.id) is None
    # default_workspace_connection_id must be cleared, not left dangling.
    refetched = await harness.installations.get(installation.id)
    assert refetched is not None and refetched.default_workspace_connection_id is None
    entry = harness.audit_log.entries_for_action("installation.workspace_unlinked")[-1]
    assert entry.result_status.value == "success"


@pytest.mark.asyncio
async def test_unlink_workspace_unknown_link_is_not_found(
    harness: AccountLinkingHarness,
) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)

    with pytest.raises(NotFoundError):
        await harness.unlink_workspace.execute(
            channel_installation_id=installation.id,
            workspace_connection_id=uuid4(),
            correlation_id="corr-1",
        )


@pytest.mark.asyncio
async def test_unlink_workspace_unknown_installation_is_not_found(
    harness: AccountLinkingHarness,
) -> None:
    with pytest.raises(NotFoundError):
        await harness.unlink_workspace.execute(
            channel_installation_id=uuid4(),
            workspace_connection_id=uuid4(),
            correlation_id="corr-1",
        )


# --- TestChannelConnection -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_test_success(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)

    result = await harness.test_connection.execute(
        channel_installation_id=installation.id, correlation_id="corr-1"
    )

    assert result.is_valid is True
    entry = harness.audit_log.entries_for_action("installation.connection_tested")[-1]
    assert entry.result_status.value == "success"


@pytest.mark.asyncio
async def test_connection_test_failure_is_reported_not_raised(
    harness: AccountLinkingHarness,
) -> None:
    """A failed connectivity check is a normal result, not a DomainError — the caller wants to
    display "invalid" in the UI, not get an exception."""
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)
    harness.validator.is_valid = False
    harness.validator.error_message = "token revoked"

    result = await harness.test_connection.execute(
        channel_installation_id=installation.id, correlation_id="corr-1"
    )

    assert result.is_valid is False
    assert result.error_message == "token revoked"
    entry = harness.audit_log.entries_for_action("installation.connection_tested")[-1]
    assert entry.result_status.value == "failure"
    assert entry.error_code == ChannelCredentialInvalidError.error_code


@pytest.mark.asyncio
async def test_connection_test_on_inactive_installation_is_blocked(
    harness: AccountLinkingHarness,
) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)
    await harness.revoke_installation.execute(
        channel_installation_id=installation.id, correlation_id="corr-revoke"
    )

    with pytest.raises(IntegrationInactiveError):
        await harness.test_connection.execute(
            channel_installation_id=installation.id, correlation_id="corr-1"
        )


# --- Activate / Deactivate -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deactivate_then_activate_round_trip(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)

    deactivated = await harness.deactivate_installation.execute(
        channel_installation_id=installation.id, correlation_id="corr-1"
    )
    assert deactivated.status is InstallationStatus.INACTIVE
    assert harness.audit_log.entries_for_action("installation.disabled")

    reactivated = await harness.activate_installation.execute(
        channel_installation_id=installation.id, correlation_id="corr-2"
    )
    assert reactivated.status is InstallationStatus.ACTIVE
    assert harness.audit_log.entries_for_action("installation.enabled")


@pytest.mark.asyncio
async def test_activate_does_not_resurrect_a_revoked_installation(
    harness: AccountLinkingHarness,
) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)
    await harness.revoke_installation.execute(
        channel_installation_id=installation.id, correlation_id="corr-revoke"
    )

    with pytest.raises(InstallationRevokedError):
        await harness.activate_installation.execute(
            channel_installation_id=installation.id, correlation_id="corr-1"
        )

    # Revocation's own action name ("installation.deactivated") must not collide with the new
    # reversible-disable action name ("installation.disabled") — regression guard for the naming
    # decision documented in DeactivateInstallation's docstring.
    assert harness.audit_log.entries_for_action("installation.deactivated")
    assert not harness.audit_log.entries_for_action("installation.disabled")


@pytest.mark.asyncio
async def test_deactivate_a_revoked_installation_also_rejected(
    harness: AccountLinkingHarness,
) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)
    await harness.revoke_installation.execute(
        channel_installation_id=installation.id, correlation_id="corr-revoke"
    )

    with pytest.raises(InstallationRevokedError):
        await harness.deactivate_installation.execute(
            channel_installation_id=installation.id, correlation_id="corr-1"
        )


async def _seed_workspace(
    harness: AccountLinkingHarness, customer_id: UUID, prom_instance_id: UUID
) -> WorkspaceConnection:
    workspace = WorkspaceConnection(
        id=uuid4(),
        customer_id=customer_id,
        prom_instance_id=prom_instance_id,
        prom_workspace_id="ws-1",
        prom_workspace_slug="acme",
        name="Acme Workspace",
        created_at=harness.clock.now(),
        updated_at=harness.clock.now(),
    )
    await harness.workspace_connections.add(workspace)
    return workspace
