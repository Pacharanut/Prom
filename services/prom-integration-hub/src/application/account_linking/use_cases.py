"""Account-linking use cases — application layer. Orchestrates domain entities and ports only;
must stay framework-free (see AGENTS.md dependency-direction rule). Every use case is a
dataclass of injected ports with a single `execute` method — composition/wiring happens in
apps/api, never here.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn
from uuid import UUID, uuid4

from src.application.account_linking.ports import (
    AuditLogRepository,
    ChannelCredentialValidator,
    ChannelInstallationRepository,
    Clock,
    DelegatedCredentialRepository,
    LinkedIdentityRepository,
    LinkingCodeRepository,
    PromInstanceRepository,
    RateLimiter,
    SecretProvider,
    ValidationResult,
    WorkspaceConnectionRepository,
)
from src.application.prom_api.ports import PromApiClient
from src.application.shared.audit_writer import AuditWriter
from src.domain.audit.entities import ResultStatus
from src.domain.identities.entities import (
    DelegatedCredential,
    IdentityStatus,
    LinkedIdentity,
    LinkingCode,
)
from src.domain.integrations.entities import (
    ChannelInstallation,
    ChannelType,
    InstallationStatus,
    InstallationWorkspace,
)
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import (
    ChannelCredentialInvalidError,
    DomainError,
    IdentityAlreadyLinkedError,
    InstallationHasLinkedIdentitiesError,
    InstallationNotRevokedError,
    InstallationRevokedError,
    IntegrationInactiveError,
    LinkingCodeExpiredError,
    LinkingCodeInvalidError,
    NotFoundError,
    PromCredentialInvalidError,
    RateLimitedError,
    TenantIsolationError,
    WorkspaceNotAllowedError,
)

_LINKING_CODE_TTL = timedelta(minutes=10)
_MAX_CODE_GENERATION_ATTEMPTS = 5
_LINKING_ATTEMPT_LIMIT = 5
_LINKING_ATTEMPT_WINDOW_SECONDS = 600


def _generate_six_digit_code() -> str:
    """Cryptographically secure 6-digit code — deliberately not `random`, see Gate 0 finding
    that the LINE prototype's `random.choices` RNG was not fit for this purpose."""
    return f"{secrets.randbelow(1_000_000):06d}"


@dataclass
class CreateChannelInstallation:
    installations: ChannelInstallationRepository
    validator: ChannelCredentialValidator
    secrets_provider: SecretProvider
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        customer_id: UUID,
        channel_type: ChannelType,
        display_name: str,
        credentials: dict[str, str],
        correlation_id: str,
    ) -> ChannelInstallation:
        audit = AuditWriter(self.audit_log, self.clock)
        result = await self.validator.validate(channel_type, credentials)
        if not result.is_valid or result.external_account_id is None:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=None,
                installation_id=None,
                channel_type=channel_type,
                channel_user_id=None,
                prom_user_id=None,
                action="installation.activation_failed",
                target_type="channel_installation",
                target_id=None,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=ChannelCredentialInvalidError.error_code,
            )
            raise ChannelCredentialInvalidError(
                result.error_message or "channel credentials failed validation"
            )

        now = self.clock.now()
        installation = ChannelInstallation(
            id=uuid4(),
            customer_id=customer_id,
            channel_type=channel_type,
            display_name=display_name,
            status=InstallationStatus.ACTIVE,
            external_account_id=result.external_account_id,
            encrypted_credentials=self.secrets_provider.encrypt(
                json.dumps(credentials, sort_keys=True).encode("utf-8")
            ),
            default_workspace_connection_id=None,
            created_at=now,
            updated_at=now,
        )
        await self.installations.add(installation)
        await audit.write(
            customer_id=customer_id,
            prom_instance_id=None,
            installation_id=installation.id,
            channel_type=channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.activated",
            target_type="channel_installation",
            target_id=str(installation.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return installation


@dataclass
class RegisterWorkspaceConnection:
    """Get-or-create a WorkspaceConnection for one (prom_instance_id, prom_workspace_id) pair.

    Promoted to a proper use case in Gate 8 once `add()` gained real business logic (an
    idempotency/update branch) rather than being a pure passthrough insert -- the Integrations UI
    needs to call this every time an admin opens the page for a workspace, not just once, so a
    second call for the same workspace must not 500. On a conflict, the existing row's `name`/
    `prom_workspace_slug` are overwritten with the caller's values (Plane's workspace is the
    source of truth for both, Hub only mirrors them) rather than left stale."""

    workspace_connections: WorkspaceConnectionRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        customer_id: UUID,
        prom_instance_id: UUID,
        prom_workspace_id: str,
        prom_workspace_slug: str,
        name: str,
        correlation_id: str,
    ) -> tuple[WorkspaceConnection, bool]:
        audit = AuditWriter(self.audit_log, self.clock)
        now = self.clock.now()
        candidate = WorkspaceConnection(
            id=uuid4(),
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            prom_workspace_id=prom_workspace_id,
            prom_workspace_slug=prom_workspace_slug,
            name=name,
            created_at=now,
            updated_at=now,
        )
        workspace, was_created = await self.workspace_connections.add(candidate)

        await audit.write(
            customer_id=customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=None,
            channel_type=None,
            channel_user_id=None,
            prom_user_id=None,
            action="workspace_connection.registered"
            if was_created
            else ("workspace_connection.updated"),
            target_type="workspace_connection",
            target_id=str(workspace.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return workspace, was_created


@dataclass
class LinkInstallationToWorkspace:
    installations: ChannelInstallationRepository
    workspace_connections: WorkspaceConnectionRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        channel_installation_id: UUID,
        workspace_connection_id: UUID,
        is_default: bool,
        correlation_id: str,
    ) -> InstallationWorkspace:
        audit = AuditWriter(self.audit_log, self.clock)

        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")

        workspace = await self.workspace_connections.get(workspace_connection_id)
        if workspace is None:
            raise NotFoundError(f"workspace_connection {workspace_connection_id} not found")

        if workspace.customer_id != installation.customer_id:
            await audit.write(
                customer_id=installation.customer_id,
                prom_instance_id=workspace.prom_instance_id,
                installation_id=channel_installation_id,
                channel_type=installation.channel_type,
                channel_user_id=None,
                prom_user_id=None,
                action="installation.link_workspace_failed",
                target_type="workspace_connection",
                target_id=str(workspace_connection_id),
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=TenantIsolationError.error_code,
            )
            raise TenantIsolationError(
                "workspace_connection belongs to a different customer than the channel_installation"
            )

        link = InstallationWorkspace(
            id=uuid4(),
            channel_installation_id=channel_installation_id,
            workspace_connection_id=workspace_connection_id,
            created_at=self.clock.now(),
        )
        await self.installations.add_workspace_link(link)

        if is_default:
            installation.default_workspace_connection_id = workspace_connection_id
            installation.updated_at = self.clock.now()
            await self.installations.save(installation)

        await audit.write(
            customer_id=installation.customer_id,
            prom_instance_id=workspace.prom_instance_id,
            installation_id=channel_installation_id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.workspace_linked",
            target_type="workspace_connection",
            target_id=str(workspace_connection_id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return link


@dataclass
class StartAccountLinking:
    installations: ChannelInstallationRepository
    prom_instances: PromInstanceRepository
    delegated_credentials: DelegatedCredentialRepository
    linking_codes: LinkingCodeRepository
    prom_api_client: PromApiClient
    secrets_provider: SecretProvider
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        channel_installation_id: UUID,
        delegated_token: str,
        default_workspace_connection_id: UUID | None,
        correlation_id: str,
    ) -> LinkingCode:
        audit = AuditWriter(self.audit_log, self.clock)

        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")

        instance = await self.prom_instances.get(prom_instance_id)
        if instance is None:
            raise NotFoundError(f"prom_instance {prom_instance_id} not found")

        async def _fail_start(error: DomainError) -> NoReturn:
            await audit.write(
                customer_id=installation.customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=channel_installation_id,
                channel_type=installation.channel_type,
                channel_user_id=None,
                prom_user_id=prom_user_id,
                action="linking_code.start_failed",
                target_type="channel_installation",
                target_id=str(channel_installation_id),
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        if installation.status != InstallationStatus.ACTIVE:
            await _fail_start(IntegrationInactiveError("channel installation is not active"))

        if installation.customer_id != instance.customer_id:
            await _fail_start(
                TenantIsolationError(
                    "channel_installation and prom_instance belong to different customers"
                )
            )

        if default_workspace_connection_id is not None:
            link = await self.installations.get_workspace_link(
                channel_installation_id, default_workspace_connection_id
            )
            if link is None:
                await _fail_start(
                    WorkspaceNotAllowedError("workspace is not linked to this channel installation")
                )

        try:
            profile = await self.prom_api_client.get_current_user(
                instance.base_url, delegated_token
            )
        except DomainError as exc:
            await _fail_start(exc)

        if profile.id != prom_user_id:
            await _fail_start(
                PromCredentialInvalidError(
                    "delegated token does not belong to the expected prom_user_id"
                )
            )

        now = self.clock.now()
        credential = DelegatedCredential(
            id=uuid4(),
            customer_id=installation.customer_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            encrypted_token=self.secrets_provider.encrypt(delegated_token.encode("utf-8")),
            created_at=now,
            updated_at=now,
            rotated_at=None,
        )
        await self.delegated_credentials.upsert(credential)

        existing = await self.linking_codes.get_active_for_user(
            channel_installation_id, prom_user_id
        )
        if existing is not None:
            existing.used_at = now
            await self.linking_codes.save(existing)

        active_codes = set(await self.linking_codes.list_active_codes(channel_installation_id))
        code_value = _generate_six_digit_code()
        attempts = 1
        while code_value in active_codes and attempts < _MAX_CODE_GENERATION_ATTEMPTS:
            code_value = _generate_six_digit_code()
            attempts += 1
        if code_value in active_codes:
            await _fail_start(
                RateLimitedError("could not generate a unique linking code after several attempts")
            )

        linking_code = LinkingCode(
            id=uuid4(),
            customer_id=installation.customer_id,
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            channel_installation_id=channel_installation_id,
            code=code_value,
            default_workspace_connection_id=default_workspace_connection_id,
            expires_at=now + _LINKING_CODE_TTL,
            used_at=None,
            created_at=now,
        )
        await self.linking_codes.add(linking_code)
        await audit.write(
            customer_id=installation.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=channel_installation_id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=prom_user_id,
            action="linking_code.created",
            target_type="linking_code",
            target_id=str(linking_code.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return linking_code


@dataclass
class ConsumeLinkingCode:
    installations: ChannelInstallationRepository
    linking_codes: LinkingCodeRepository
    linked_identities: LinkedIdentityRepository
    rate_limiter: RateLimiter
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        channel_installation_id: UUID,
        channel_type: ChannelType,
        channel_user_id: str,
        submitted_code: str,
        correlation_id: str,
    ) -> LinkedIdentity:
        audit = AuditWriter(self.audit_log, self.clock)

        async def _fail_consume(error: DomainError) -> NoReturn:
            await audit.write(
                customer_id=None,
                prom_instance_id=None,
                installation_id=channel_installation_id,
                channel_type=channel_type,
                channel_user_id=channel_user_id,
                prom_user_id=None,
                action="identity.link_failed",
                target_type="linked_identity",
                target_id=None,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        allowed = await self.rate_limiter.check_and_increment(
            key=f"linking-attempt:{channel_installation_id}:{channel_user_id}",
            limit=_LINKING_ATTEMPT_LIMIT,
            window_seconds=_LINKING_ATTEMPT_WINDOW_SECONDS,
        )
        if not allowed:
            await _fail_consume(RateLimitedError("too many linking attempts, try again later"))

        linking_code = await self.linking_codes.get_by_code(channel_installation_id, submitted_code)
        if linking_code is None or linking_code.used_at is not None:
            await _fail_consume(LinkingCodeInvalidError("linking code is invalid or already used"))

        now = self.clock.now()
        if linking_code.expires_at <= now:
            await _fail_consume(LinkingCodeExpiredError("linking code has expired"))

        identity = LinkedIdentity(
            id=uuid4(),
            customer_id=linking_code.customer_id,
            channel_installation_id=channel_installation_id,
            channel_type=channel_type,
            channel_user_id=channel_user_id,
            prom_instance_id=linking_code.prom_instance_id,
            prom_user_id=linking_code.prom_user_id,
            default_workspace_connection_id=linking_code.default_workspace_connection_id,
            status=IdentityStatus.ACTIVE,
            linked_at=now,
            unlinked_at=None,
        )
        try:
            await self.linked_identities.add(identity)
        except IdentityAlreadyLinkedError as exc:
            await _fail_consume(exc)

        linking_code.used_at = now
        await self.linking_codes.save(linking_code)

        await audit.write(
            customer_id=linking_code.customer_id,
            prom_instance_id=linking_code.prom_instance_id,
            installation_id=channel_installation_id,
            channel_type=channel_type,
            channel_user_id=channel_user_id,
            prom_user_id=linking_code.prom_user_id,
            action="identity.linked",
            target_type="linked_identity",
            target_id=str(identity.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return identity


@dataclass
class UnlinkIdentity:
    linked_identities: LinkedIdentityRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(self, *, linked_identity_id: UUID, correlation_id: str) -> LinkedIdentity:
        identity = await self.linked_identities.get(linked_identity_id)
        if identity is None:
            raise NotFoundError(f"linked_identity {linked_identity_id} not found")

        now = self.clock.now()
        identity.status = IdentityStatus.UNLINKED
        identity.unlinked_at = now
        await self.linked_identities.save(identity)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=identity.customer_id,
            prom_instance_id=identity.prom_instance_id,
            installation_id=identity.channel_installation_id,
            channel_type=identity.channel_type,
            channel_user_id=identity.channel_user_id,
            prom_user_id=identity.prom_user_id,
            action="identity.unlinked",
            target_type="linked_identity",
            target_id=str(identity.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return identity


@dataclass
class RevokeInstallation:
    installations: ChannelInstallationRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self, *, channel_installation_id: UUID, correlation_id: str
    ) -> ChannelInstallation:
        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")

        installation.status = InstallationStatus.REVOKED
        installation.updated_at = self.clock.now()
        await self.installations.save(installation)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=installation.customer_id,
            prom_instance_id=None,
            installation_id=installation.id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.deactivated",
            target_type="channel_installation",
            target_id=str(installation.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return installation


@dataclass
class DeleteChannelInstallation:
    """Hard-delete a `revoked` installation an admin created by mistake (wrong/expired
    credentials, typo'd display name) and never actually used — distinct from `RevokeInstallation`
    (a reversible-in-spirit status flip, kept for real audit history). Only reachable from
    `revoked` (mirrors `InstallationRevokedError`'s one-way direction the other way), and only when
    zero `LinkedIdentity` rows exist for it — an installation any real user ever linked through is
    never silently removable, no matter its current status."""

    installations: ChannelInstallationRepository
    linked_identities: LinkedIdentityRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(self, *, channel_installation_id: UUID, correlation_id: str) -> None:
        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")

        if installation.status != InstallationStatus.REVOKED:
            raise InstallationNotRevokedError(
                "installation must be revoked before it can be deleted"
            )

        if await self.linked_identities.exists_for_installation(channel_installation_id):
            raise InstallationHasLinkedIdentitiesError(
                "installation has linked identity history and cannot be deleted"
            )

        await self.installations.delete(channel_installation_id)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=installation.customer_id,
            prom_instance_id=None,
            installation_id=installation.id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.deleted",
            target_type="channel_installation",
            target_id=str(installation.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )


@dataclass
class UnlinkWorkspaceFromInstallation:
    """Gate 8: remove one installation<->workspace mapping (the "workspace mappings" admin
    action) — distinct from `RevokeInstallation` (which deactivates the whole installation) and
    from anything in Gate 3, which only ever *added* links."""

    installations: ChannelInstallationRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        channel_installation_id: UUID,
        workspace_connection_id: UUID,
        correlation_id: str,
    ) -> None:
        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")

        removed = await self.installations.remove_workspace_link(
            channel_installation_id, workspace_connection_id
        )
        if not removed:
            raise NotFoundError(
                f"workspace_connection {workspace_connection_id} is not linked to "
                f"channel_installation {channel_installation_id}"
            )

        # If the unlinked workspace was this installation's default, clear the default rather
        # than leaving it pointing at a mapping that no longer exists.
        if installation.default_workspace_connection_id == workspace_connection_id:
            installation.default_workspace_connection_id = None
            installation.updated_at = self.clock.now()
            await self.installations.save(installation)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=installation.customer_id,
            prom_instance_id=None,
            installation_id=channel_installation_id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.workspace_unlinked",
            target_type="workspace_connection",
            target_id=str(workspace_connection_id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )


@dataclass
class TestChannelConnection:
    """Gate 8: the "Test Connection" admin action — a pure connectivity check against the
    channel's own API using the credentials already on file, never Prom's. Does not persist
    anything (unlike `CreateChannelInstallation`, which validates *before* saving); audits either
    outcome so a failed test is visible in the audit trail, not just the UI."""

    installations: ChannelInstallationRepository
    validator: ChannelCredentialValidator
    secrets_provider: SecretProvider
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self, *, channel_installation_id: UUID, correlation_id: str
    ) -> ValidationResult:
        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")
        if installation.status is not InstallationStatus.ACTIVE:
            raise IntegrationInactiveError(
                f"channel_installation {channel_installation_id} is not active"
            )

        credentials: dict[str, str] = json.loads(
            self.secrets_provider.decrypt(installation.encrypted_credentials).decode("utf-8")
        )
        result = await self.validator.validate(installation.channel_type, credentials)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=installation.customer_id,
            prom_instance_id=None,
            installation_id=channel_installation_id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.connection_tested",
            target_type="channel_installation",
            target_id=str(channel_installation_id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS if result.is_valid else ResultStatus.FAILURE,
            error_code=None if result.is_valid else ChannelCredentialInvalidError.error_code,
        )
        return result


@dataclass
class ActivateInstallation:
    """Gate 8: reversible enable, distinct from `RevokeInstallation` (one-way,
    status=revoked). Toggles active<->inactive only — cannot resurrect a revoked installation
    (raises `InstallationRevokedError`; create a new installation instead, same as how a revoked
    LINE/Discord credential can't be un-revoked on the channel platform's own side either)."""

    installations: ChannelInstallationRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self, *, channel_installation_id: UUID, correlation_id: str
    ) -> ChannelInstallation:
        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")
        if installation.status is InstallationStatus.REVOKED:
            raise InstallationRevokedError(
                f"channel_installation {channel_installation_id} is revoked and cannot be "
                "reactivated"
            )

        installation.status = InstallationStatus.ACTIVE
        installation.updated_at = self.clock.now()
        await self.installations.save(installation)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=installation.customer_id,
            prom_instance_id=None,
            installation_id=installation.id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.enabled",
            target_type="channel_installation",
            target_id=str(installation.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return installation


@dataclass
class DeactivateInstallation:
    """Gate 8: reversible disable — the counterpart to `ActivateInstallation`. Distinct action
    name from `RevokeInstallation`'s `installation.deactivated` (a pre-existing, permanent action
    this gate must not collide with): this one is `installation.disabled`, and unlike revoke,
    a `DeactivateInstallation` can always be undone by `ActivateInstallation`."""

    installations: ChannelInstallationRepository
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self, *, channel_installation_id: UUID, correlation_id: str
    ) -> ChannelInstallation:
        installation = await self.installations.get(channel_installation_id)
        if installation is None:
            raise NotFoundError(f"channel_installation {channel_installation_id} not found")
        if installation.status is InstallationStatus.REVOKED:
            raise InstallationRevokedError(
                f"channel_installation {channel_installation_id} is already revoked"
            )

        installation.status = InstallationStatus.INACTIVE
        installation.updated_at = self.clock.now()
        await self.installations.save(installation)

        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=installation.customer_id,
            prom_instance_id=None,
            installation_id=installation.id,
            channel_type=installation.channel_type,
            channel_user_id=None,
            prom_user_id=None,
            action="installation.disabled",
            target_type="channel_installation",
            target_id=str(installation.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return installation
