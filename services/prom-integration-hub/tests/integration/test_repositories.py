"""Integration tests against a real Postgres database. Run with: pytest -m integration
(requires `docker compose -f deployments/docker-compose.yml up -d postgres redis` and
`alembic upgrade head` first — see README).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import (
    ChannelInstallation,
    ChannelType,
    InstallationStatus,
    InstallationWorkspace,
)
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import IdentityAlreadyLinkedError

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_customer_and_instance(
    session: AsyncSession,
) -> tuple[Customer, PromInstance]:
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(session).add(customer)

    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="Acme Prom",
        base_url="https://prom.acme.example",
        status=InstanceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresPromInstanceRepository(session).add(instance)
    return customer, instance


@pytest.mark.asyncio
async def test_customer_round_trip(db_session: AsyncSession) -> None:
    repository = PostgresCustomerRepository(db_session)
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )

    await repository.add(customer)
    fetched = await repository.get(customer.id)

    assert fetched is not None
    assert fetched.id == customer.id
    assert fetched.name == "Acme"
    assert fetched.status is CustomerStatus.ACTIVE


@pytest.mark.asyncio
async def test_workspace_connection_add_is_get_or_create_and_updates_on_conflict(
    db_session: AsyncSession,
) -> None:
    """`add()` on a duplicate (prom_instance_id, prom_workspace_id) used to raise a raw
    `IntegrityError` — changed in Gate 8 to a SAVEPOINT-wrapped get-or-create (same pattern as
    `PostgresLinkedIdentityRepository.add`), since the Integrations UI needs to safely call this
    every time an admin opens the page for a workspace. The second call's `name`/
    `prom_workspace_slug` must win (Plane's workspace is the source of truth, Hub only mirrors
    it) — and the surrounding transaction must remain usable afterward, same SAVEPOINT-recovery
    assertion style as every other conflict-translation test in this file.
    """
    customer, instance = await _seed_customer_and_instance(db_session)
    repository = PostgresWorkspaceConnectionRepository(db_session)

    first = WorkspaceConnection(
        id=uuid4(),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_workspace_id="ws-1",
        prom_workspace_slug="acme",
        name="Acme Workspace",
        created_at=_NOW,
        updated_at=_NOW,
    )
    created, was_created = await repository.add(first)
    assert was_created is True
    assert created.id == first.id

    conflicting = WorkspaceConnection(
        id=uuid4(),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_workspace_id="ws-1",
        prom_workspace_slug="acme-renamed",
        name="Acme Workspace Renamed",
        created_at=_NOW,
        updated_at=_NOW,
    )
    resolved, was_created_again = await repository.add(conflicting)
    assert was_created_again is False
    # The *existing* row's id is preserved (not the conflicting candidate's) -- only its mutable
    # fields are updated in place.
    assert resolved.id == first.id
    assert resolved.name == "Acme Workspace Renamed"
    assert resolved.prom_workspace_slug == "acme-renamed"

    fetched = await repository.get(first.id)
    assert fetched is not None
    assert fetched.name == "Acme Workspace Renamed"

    # SAVEPOINT recovery didn't poison the outer transaction.
    recovery_customer = Customer(
        id=uuid4(),
        name="Post-Conflict",
        status=CustomerStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresCustomerRepository(db_session).add(recovery_customer)


@pytest.mark.asyncio
async def test_relinking_active_identity_raises_typed_domain_error(
    db_session: AsyncSession,
) -> None:
    """The partial unique index `uq_linked_identities_active_prom_user` rejects a second active
    LinkedIdentity for the same prom_user_id on an installation — PostgresLinkedIdentityRepository
    must translate that IntegrityError into IdentityAlreadyLinkedError (not leak the raw
    IntegrityError), and the surrounding session/transaction must remain usable afterward (the
    SAVEPOINT in `add()` exists precisely so a caller can still write an audit-log row in the same
    transaction after this failure — asserted here by successfully adding a Customer afterward).
    """
    customer, instance = await _seed_customer_and_instance(db_session)
    installation = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE OA",
        status=InstallationStatus.ACTIVE,
        external_account_id="line-account-1",
        encrypted_credentials=b"ciphertext",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresChannelInstallationRepository(db_session).add(installation)

    repository = PostgresLinkedIdentityRepository(db_session)
    first = LinkedIdentity(
        id=uuid4(),
        customer_id=customer.id,
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-1",
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=_NOW,
        unlinked_at=None,
    )
    await repository.add(first)

    duplicate = LinkedIdentity(
        id=uuid4(),
        customer_id=customer.id,
        channel_installation_id=installation.id,
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-2",  # different channel_user_id, same prom_user_id
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=_NOW,
        unlinked_at=None,
    )
    with pytest.raises(IdentityAlreadyLinkedError):
        await repository.add(duplicate)

    # The savepoint rollback must not have poisoned the outer transaction.
    recovery_customer = Customer(
        id=uuid4(),
        name="Post-Failure",
        status=CustomerStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await PostgresCustomerRepository(db_session).add(recovery_customer)


@pytest.mark.asyncio
async def test_list_active_by_channel_type_filters_type_and_status(
    db_session: AsyncSession,
) -> None:
    """New in Gate 6 — apps/discord_bot/main.py's startup query. Real Postgres coverage matters
    here specifically because it's a WHERE clause on two columns (channel_type, status), and a
    fake repository can't catch a wrong column name or a stringified-enum mismatch the way a real
    query against real column types can.
    """
    customer, _ = await _seed_customer_and_instance(db_session)
    repository = PostgresChannelInstallationRepository(db_session)

    active_discord = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.DISCORD,
        display_name="Active Discord Bot",
        status=InstallationStatus.ACTIVE,
        external_account_id="discord-active",
        encrypted_credentials=b"ciphertext",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    revoked_discord = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.DISCORD,
        display_name="Revoked Discord Bot",
        status=InstallationStatus.REVOKED,
        external_account_id="discord-revoked",
        encrypted_credentials=b"ciphertext",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    active_line = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Active LINE Bot",
        status=InstallationStatus.ACTIVE,
        external_account_id="line-active",
        encrypted_credentials=b"ciphertext",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    for installation in (active_discord, revoked_discord, active_line):
        await repository.add(installation)

    result = await repository.list_active_by_channel_type(ChannelType.DISCORD)

    assert [r.id for r in result] == [active_discord.id]


@pytest.mark.asyncio
async def test_list_by_customer_returns_every_status(db_session: AsyncSession) -> None:
    """Gate 8 — the "Integrations" admin listing must show inactive/revoked installations too,
    unlike list_active_by_channel_type which is status-filtered by design."""
    customer, _ = await _seed_customer_and_instance(db_session)
    other_customer = Customer(
        id=uuid4(), name="Other Co", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await PostgresCustomerRepository(db_session).add(other_customer)
    repository = PostgresChannelInstallationRepository(db_session)

    statuses = (InstallationStatus.ACTIVE, InstallationStatus.INACTIVE, InstallationStatus.REVOKED)
    for status in statuses:
        await repository.add(
            ChannelInstallation(
                id=uuid4(),
                customer_id=customer.id,
                channel_type=ChannelType.LINE,
                display_name=f"Bot {status.value}",
                status=status,
                external_account_id=f"ext-{status.value}",
                encrypted_credentials=b"ciphertext",
                default_workspace_connection_id=None,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    await repository.add(
        ChannelInstallation(
            id=uuid4(),
            customer_id=other_customer.id,
            channel_type=ChannelType.LINE,
            display_name="Other customer's bot",
            status=InstallationStatus.ACTIVE,
            external_account_id="ext-other",
            encrypted_credentials=b"ciphertext",
            default_workspace_connection_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )

    result = await repository.list_by_customer(customer.id)

    assert len(result) == 3
    assert {r.status for r in result} == {
        InstallationStatus.ACTIVE,
        InstallationStatus.INACTIVE,
        InstallationStatus.REVOKED,
    }


@pytest.mark.asyncio
async def test_remove_workspace_link_real_delete(db_session: AsyncSession) -> None:
    customer, instance = await _seed_customer_and_instance(db_session)
    workspace_repo = PostgresWorkspaceConnectionRepository(db_session)
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
    await workspace_repo.add(workspace)

    installation_repo = PostgresChannelInstallationRepository(db_session)
    installation = ChannelInstallation(
        id=uuid4(),
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE Bot",
        status=InstallationStatus.ACTIVE,
        external_account_id="external-1",
        encrypted_credentials=b"ciphertext",
        default_workspace_connection_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await installation_repo.add(installation)

    await installation_repo.add_workspace_link(
        InstallationWorkspace(
            id=uuid4(),
            channel_installation_id=installation.id,
            workspace_connection_id=workspace.id,
            created_at=_NOW,
        )
    )

    removed = await installation_repo.remove_workspace_link(installation.id, workspace.id)
    assert removed is True
    assert await installation_repo.get_workspace_link(installation.id, workspace.id) is None

    removed_again = await installation_repo.remove_workspace_link(installation.id, workspace.id)
    assert removed_again is False


@pytest.mark.asyncio
async def test_audit_log_list_for_customer_real_ordering_and_filter(
    db_session: AsyncSession,
) -> None:
    customer, _ = await _seed_customer_and_instance(db_session)
    installation_id = uuid4()
    repository = PostgresAuditLogRepository(db_session)

    await repository.add(
        AuditLogEntry(
            id=uuid4(),
            customer_id=customer.id,
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
    )
    await repository.add(
        AuditLogEntry(
            id=uuid4(),
            customer_id=customer.id,
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
    )

    entries = await repository.list_for_customer(customer.id)
    assert [e.action for e in entries] == ["installation.disabled", "installation.activated"]

    filtered = await repository.list_for_customer(customer.id, installation_id=installation_id)
    assert len(filtered) == 2

    filtered_out = await repository.list_for_customer(customer.id, installation_id=uuid4())
    assert filtered_out == []

    limited = await repository.list_for_customer(customer.id, limit=1)
    assert len(limited) == 1
    assert limited[0].action == "installation.disabled"
