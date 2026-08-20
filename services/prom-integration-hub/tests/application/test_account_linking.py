"""Unit tests for the account-linking use cases, using in-memory fakes only (no infra)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from src.domain.identities.entities import IdentityStatus
from src.domain.integrations.entities import ChannelType
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import (
    IdentityAlreadyLinkedError,
    IntegrationInactiveError,
    LinkingCodeExpiredError,
    LinkingCodeInvalidError,
    RateLimitedError,
    TenantIsolationError,
    WorkspaceNotAllowedError,
)
from tests.application.conftest import AccountLinkingHarness

PROM_USER_ID = "prom-user-1"


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


@pytest.mark.asyncio
async def test_happy_path_creates_active_linked_identity(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    workspace = await _seed_workspace(harness, customer.id, instance.id)
    installation = await harness.create_active_installation(customer.id)

    await harness.link_workspace.execute(
        channel_installation_id=installation.id,
        workspace_connection_id=workspace.id,
        is_default=True,
        correlation_id="corr-1",
    )

    linking_code = await harness.start_linking.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        channel_installation_id=installation.id,
        delegated_token="delegated-token",
        default_workspace_connection_id=workspace.id,
        correlation_id="corr-2",
    )
    assert len(linking_code.code) == 6 and linking_code.code.isdigit()

    identity = await harness.consume_code.execute(
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-1",
        submitted_code=linking_code.code,
        correlation_id="corr-3",
    )

    assert identity.status is IdentityStatus.ACTIVE
    assert identity.prom_user_id == PROM_USER_ID
    assert identity.default_workspace_connection_id == workspace.id
    linked_entry = harness.audit_log.entries_for_action("identity.linked")[0]
    assert linked_entry.result_status.value == "success"


@pytest.mark.asyncio
async def test_otp_expiry(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    installation = await harness.create_active_installation(customer.id)

    linking_code = await harness.start_linking.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        channel_installation_id=installation.id,
        delegated_token="delegated-token",
        default_workspace_connection_id=None,
        correlation_id="corr-1",
    )

    harness.clock.advance(timedelta(minutes=11))

    with pytest.raises(LinkingCodeExpiredError):
        await harness.consume_code.execute(
            channel_installation_id=installation.id,
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-1",
            submitted_code=linking_code.code,
            correlation_id="corr-2",
        )

    failure = harness.audit_log.entries_for_action("identity.link_failed")[-1]
    assert failure.error_code == LinkingCodeExpiredError.error_code
    assert failure.result_status.value == "failure"


@pytest.mark.asyncio
async def test_otp_reuse_is_rejected(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    installation = await harness.create_active_installation(customer.id)

    linking_code = await harness.start_linking.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        channel_installation_id=installation.id,
        delegated_token="delegated-token",
        default_workspace_connection_id=None,
        correlation_id="corr-1",
    )

    await harness.consume_code.execute(
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-1",
        submitted_code=linking_code.code,
        correlation_id="corr-2",
    )

    with pytest.raises(LinkingCodeInvalidError):
        await harness.consume_code.execute(
            channel_installation_id=installation.id,
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-2",
            submitted_code=linking_code.code,
            correlation_id="corr-3",
        )

    failure = harness.audit_log.entries_for_action("identity.link_failed")[-1]
    assert failure.error_code == LinkingCodeInvalidError.error_code


@pytest.mark.asyncio
async def test_rate_limiting_blocks_after_five_attempts(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    installation = await harness.create_active_installation(customer.id)

    for _ in range(5):
        with pytest.raises(LinkingCodeInvalidError):
            await harness.consume_code.execute(
                channel_installation_id=installation.id,
                channel_type=ChannelType.LINE,
                channel_user_id="line-user-1",
                submitted_code="000000",
                correlation_id="corr-attempt",
            )

    with pytest.raises(RateLimitedError):
        await harness.consume_code.execute(
            channel_installation_id=installation.id,
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-1",
            submitted_code="000000",
            correlation_id="corr-6",
        )

    failure = harness.audit_log.entries_for_action("identity.link_failed")[-1]
    assert failure.error_code == RateLimitedError.error_code


@pytest.mark.asyncio
async def test_tenant_isolation_rejects_cross_customer_linking(
    harness: AccountLinkingHarness,
) -> None:
    customer_a = await harness.seed_customer()
    customer_b = await harness.seed_customer()
    installation = await harness.create_active_installation(customer_a.id)
    other_instance = await harness.seed_prom_instance(customer_b.id)

    with pytest.raises(TenantIsolationError):
        await harness.start_linking.execute(
            prom_instance_id=other_instance.id,
            prom_user_id=PROM_USER_ID,
            channel_installation_id=installation.id,
            delegated_token="delegated-token",
            default_workspace_connection_id=None,
            correlation_id="corr-1",
        )

    failure = harness.audit_log.entries_for_action("linking_code.start_failed")[-1]
    assert failure.error_code == TenantIsolationError.error_code


@pytest.mark.asyncio
async def test_workspace_not_allowed_when_not_linked_to_installation(
    harness: AccountLinkingHarness,
) -> None:
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    installation = await harness.create_active_installation(customer.id)
    unlinked_workspace = await _seed_workspace(harness, customer.id, instance.id)

    with pytest.raises(WorkspaceNotAllowedError):
        await harness.start_linking.execute(
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER_ID,
            channel_installation_id=installation.id,
            delegated_token="delegated-token",
            default_workspace_connection_id=unlinked_workspace.id,
            correlation_id="corr-1",
        )

    failure = harness.audit_log.entries_for_action("linking_code.start_failed")[-1]
    assert failure.error_code == WorkspaceNotAllowedError.error_code


@pytest.mark.asyncio
async def test_integration_inactive_blocks_linking(harness: AccountLinkingHarness) -> None:
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    installation = await harness.create_active_installation(customer.id)
    await harness.revoke_installation.execute(
        channel_installation_id=installation.id, correlation_id="corr-revoke"
    )

    with pytest.raises(IntegrationInactiveError):
        await harness.start_linking.execute(
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER_ID,
            channel_installation_id=installation.id,
            delegated_token="delegated-token",
            default_workspace_connection_id=None,
            correlation_id="corr-1",
        )

    failure = harness.audit_log.entries_for_action("linking_code.start_failed")[-1]
    assert failure.error_code == IntegrationInactiveError.error_code


@pytest.mark.asyncio
async def test_relinking_same_prom_user_while_active_is_rejected(
    harness: AccountLinkingHarness,
) -> None:
    """A prom_user_id that already has an *active* LinkedIdentity on an installation cannot be
    linked a second time (even from a different channel_user_id) without unlinking first — this
    is what the persistence layer's partial unique index on (channel_installation_id,
    prom_user_id) enforces for real against Postgres; the fake repository mirrors the same rule
    so this can be exercised without live infra. See PostgresLinkedIdentityRepository.add and
    tests/integration/test_repositories.py for the real-constraint counterpart.
    """
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    installation = await harness.create_active_installation(customer.id)

    first_code = await harness.start_linking.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        channel_installation_id=installation.id,
        delegated_token="delegated-token",
        default_workspace_connection_id=None,
        correlation_id="corr-1",
    )
    await harness.consume_code.execute(
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-1",
        submitted_code=first_code.code,
        correlation_id="corr-2",
    )

    second_code = await harness.start_linking.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        channel_installation_id=installation.id,
        delegated_token="delegated-token",
        default_workspace_connection_id=None,
        correlation_id="corr-3",
    )

    with pytest.raises(IdentityAlreadyLinkedError):
        await harness.consume_code.execute(
            channel_installation_id=installation.id,
            channel_type=ChannelType.LINE,
            channel_user_id="line-user-2",
            submitted_code=second_code.code,
            correlation_id="corr-4",
        )

    failure = harness.audit_log.entries_for_action("identity.link_failed")[-1]
    assert failure.error_code == IdentityAlreadyLinkedError.error_code
