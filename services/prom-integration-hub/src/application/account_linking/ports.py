"""Ports (interfaces) the account-linking use cases depend on. Implementations live in
src/adapters and src/channels; this module itself stays framework-free — only stdlib typing and
domain imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.domain.audit.entities import AuditLogEntry
from src.domain.customers.entities import Customer
from src.domain.identities.entities import DelegatedCredential, LinkedIdentity, LinkingCode
from src.domain.instances.entities import PromInstance
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationWorkspace
from src.domain.workspaces.entities import WorkspaceConnection

# PromApiClient/PromUserProfile moved to src.application.prom_api.ports in Gate 4, once the
# work_items module needed the same port — re-import from there, not redefined here.

# --- Repository ports ---------------------------------------------------------------------


class CustomerRepository(Protocol):
    async def get(self, customer_id: UUID) -> Customer | None: ...
    async def add(self, customer: Customer) -> None: ...


class PromInstanceRepository(Protocol):
    async def get(self, instance_id: UUID) -> PromInstance | None: ...
    async def add(self, instance: PromInstance) -> None: ...


class WorkspaceConnectionRepository(Protocol):
    async def get(self, workspace_connection_id: UUID) -> WorkspaceConnection | None: ...

    async def add(
        self, workspace_connection: WorkspaceConnection
    ) -> tuple[WorkspaceConnection, bool]:
        """Get-or-create on the real unique constraint over (prom_instance_id,
        prom_workspace_id) — added in Gate 8 once the Integrations UI needed to safely call this
        every time an admin opens the page for a workspace, not just once. Returns `(entity,
        was_created)`: on a fresh insert, `was_created=True` and `entity` is exactly what was
        passed in; on a conflict, the existing row is updated in place (`name`/
        `prom_workspace_slug` overwritten with the caller's values -- Plane's workspace is the
        source of truth, Hub only mirrors it, so a rename in Plane must propagate here, never the
        other way around) and `was_created=False`. Mirrors the same `tuple[Entity, bool]`
        "was this a replay" shape `FakePromApiClient.create_work_item`/`CreateWorkItem` already
        use for Prom's own external_id dedup (Gate 4) -- same idea, same shape, different table.
        """
        ...

    async def save(self, workspace_connection: WorkspaceConnection) -> None:
        """Added in Gate 7 for `SetWorkspaceWebhookSecret` — every field before
        `webhook_secret_encrypted` was create-once/immutable, so no use case needed an update
        path until now."""
        ...

    async def get_by_external_id(
        self, prom_instance_id: UUID, prom_workspace_id: str
    ) -> WorkspaceConnection | None:
        """Look up by the real unique key (prom_instance_id, prom_workspace_id) -- added in Gate
        8 alongside the get-or-create `add()` above, which uses this internally on conflict."""
        ...


class ChannelInstallationRepository(Protocol):
    async def get(self, installation_id: UUID) -> ChannelInstallation | None: ...
    async def add(self, installation: ChannelInstallation) -> None: ...
    async def save(self, installation: ChannelInstallation) -> None: ...
    async def add_workspace_link(self, link: InstallationWorkspace) -> None: ...
    async def get_workspace_link(
        self, channel_installation_id: UUID, workspace_connection_id: UUID
    ) -> InstallationWorkspace | None: ...
    async def list_workspaces(
        self, channel_installation_id: UUID
    ) -> list[InstallationWorkspace]: ...

    async def list_active_by_channel_type(
        self, channel_type: ChannelType
    ) -> list[ChannelInstallation]:
        """All ACTIVE installations of one channel type across every customer — used by
        `apps/discord_bot/main.py` at process startup to decide which bot Clients to start (one
        per installation). Added in Gate 6; not needed by LINE since its webhook route resolves
        one installation per HTTP request instead of enumerating all of them upfront."""
        ...

    async def list_by_customer(self, customer_id: UUID) -> list[ChannelInstallation]:
        """Every installation (any status) belonging to one customer — added in Gate 8 for the
        "Integrations" admin listing page. Unlike `list_active_by_channel_type`, deliberately not
        status-filtered: an admin managing integrations needs to see inactive/revoked ones too,
        not just active ones."""
        ...

    async def delete(self, installation_id: UUID) -> None:
        """Hard-delete one `revoked` installation and its own workspace-link/linking-code rows —
        `DeleteChannelInstallation` is the only caller, and only after confirming REVOKED status
        and zero `LinkedIdentity` rows, so by the time this runs there is nothing here worth
        keeping (`audit_logs.installation_id` is a denormalized, FK-free column, so audit history
        survives regardless)."""
        ...

    async def remove_workspace_link(
        self, channel_installation_id: UUID, workspace_connection_id: UUID
    ) -> bool:
        """Delete one installation<->workspace mapping. Returns True if a row existed and was
        removed, False if there was nothing to delete (caller maps that to a 404) — added in
        Gate 8; no prior use case needed to *remove* a link, only add/list/get one."""
        ...


class LinkedIdentityRepository(Protocol):
    async def get(self, linked_identity_id: UUID) -> LinkedIdentity | None: ...
    async def add(self, identity: LinkedIdentity) -> None: ...
    async def save(self, identity: LinkedIdentity) -> None: ...
    async def get_active_by_channel_user(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> LinkedIdentity | None:
        """Resolve "who is this LINE/Discord user" on every inbound webhook message (not just
        during the linking flow itself) — added in Gate 5 once a channel adapter needed it.
        Covered by the same partial unique index that already enforces one active identity per
        (channel_installation_id, channel_user_id)."""
        ...

    async def list_all_active_with_default_workspace(self) -> list[LinkedIdentity]:
        """Every active identity, across every customer, that has a
        `default_workspace_connection_id` set — added in Gate 7 for the due-date/overdue
        scheduled check, which needs a workspace to query per identity and has no other way to
        pick one (identities without a default are skipped entirely, not guessed at — see
        CheckDueDateNotifications). Global/unpaginated: a deliberate Phase 1 scale limitation for
        a background job, not an oversight — see README."""
        ...

    async def list_active_by_prom_user(
        self, prom_instance_id: UUID, prom_user_id: str
    ) -> list[LinkedIdentity]:
        """Every active identity (across all channel installations/types) for one Prom user on
        one instance — added in Gate 7: a Prom webhook event names a `prom_user_id`, and that
        person may be linked on both LINE and Discord (or multiple installations of the same
        channel) — notify all of them, not just one, per §8.6 "Private message/DM เป็นช่องทางหลัก"
        (not "pick one channel")."""
        ...

    async def exists_for_installation(self, channel_installation_id: UUID) -> bool:
        """Any `LinkedIdentity` row at all (active or unlinked) for one installation — added for
        `DeleteChannelInstallation`'s safety check: real account-linking history, even if since
        unlinked, is a reason to refuse a hard delete, not just an active link."""
        ...


class LinkingCodeRepository(Protocol):
    async def add(self, linking_code: LinkingCode) -> None: ...
    async def save(self, linking_code: LinkingCode) -> None: ...
    async def get_by_code(self, channel_installation_id: UUID, code: str) -> LinkingCode | None:
        """Look up a code within one installation's scope, regardless of used/expired state —
        the use case decides how to interpret used_at/expires_at."""
        ...

    async def list_active_codes(self, channel_installation_id: UUID) -> list[str]:
        """Currently-active (unused, unexpired) code values within one installation — used only
        to avoid generating a colliding code, not a security boundary by itself."""
        ...

    async def get_active_for_user(
        self, channel_installation_id: UUID, prom_user_id: str
    ) -> LinkingCode | None:
        """The current active (unused, unexpired) code for this (installation, prom_user_id)
        pair, if any — so StartAccountLinking can invalidate it before issuing a new one."""
        ...


class DelegatedCredentialRepository(Protocol):
    async def get(
        self, prom_instance_id: UUID, prom_user_id: str
    ) -> DelegatedCredential | None: ...
    async def upsert(self, credential: DelegatedCredential) -> None: ...

    async def get_any_for_instance(self, prom_instance_id: UUID) -> DelegatedCredential | None:
        """Added in Gate 7 for `ProcessPromWebhookEvent`'s comment-notification path: an
        `issue_comment` webhook event doesn't carry the parent issue's assignees (unlike an
        `issue` event's expanded payload), so determining who to notify requires a `GetWorkItem`
        call — which needs *some* valid delegated token for the instance, before we know which
        specific user we're even notifying yet (chicken-and-egg). Any active linked user's
        credential works for a read-only lookup; if none exists yet (no one on this Prom instance
        has completed account linking), comment notifications are skipped for that instance —
        documented limitation, not a crash."""
        ...


class AuditLogRepository(Protocol):
    async def add(self, entry: AuditLogEntry) -> None: ...

    async def list_for_customer(
        self,
        customer_id: UUID,
        *,
        installation_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLogEntry]:
        """Most-recent-first audit trail for one customer, optionally narrowed to one
        installation — added in Gate 8 for the admin audit-visibility page. Deliberately no
        `workspace_connection_id` filter: `AuditLogEntry` has no such field (only
        `prom_instance_id`/`installation_id`) — filtering by `target_id` would only catch entries
        where a workspace_connection was the *direct* action target (e.g.
        `installation.workspace_linked`), silently missing every other entry that's merely
        *related* to that workspace, which would be misleading rather than useful. Plain
        limit/offset, not cursor-based: this is a straightforward `ORDER BY occurred_at DESC
        LIMIT/OFFSET` query, not worth inventing a cursor protocol for (unlike
        `ListMyWorkItems`/`list_work_items`, which wraps Prom's own real cursor-paginated API)."""
        ...


# --- Supporting ports -----------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, UTC, timezone-aware."""
        ...


class SecretProvider(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...


@dataclass
class ValidationResult:
    is_valid: bool
    external_account_id: str | None
    error_message: str | None


class ChannelCredentialValidator(Protocol):
    async def validate(
        self, channel_type: ChannelType, credentials: dict[str, str]
    ) -> ValidationResult: ...


class RateLimiter(Protocol):
    async def check_and_increment(self, key: str, limit: int, window_seconds: int) -> bool:
        """Returns True if this attempt is allowed, False if the caller is over limit."""
        ...
