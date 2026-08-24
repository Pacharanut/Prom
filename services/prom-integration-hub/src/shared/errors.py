"""Domain/application error catalog. Framework-free — presenters (API routers, LINE/Discord
channel adapters) translate these into HTTP responses or channel-specific user copy. Domain and
application code must never return raw Thai/English user-facing strings; only these typed errors.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all typed domain/application errors."""

    error_code: str = "DOMAIN_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LinkingCodeInvalidError(DomainError):
    error_code = "LINKING_CODE_INVALID"


class LinkingCodeExpiredError(DomainError):
    error_code = "LINKING_CODE_EXPIRED"


class RateLimitedError(DomainError):
    error_code = "RATE_LIMITED"


class IntegrationInactiveError(DomainError):
    error_code = "INTEGRATION_INACTIVE"


class WorkspaceNotAllowedError(DomainError):
    error_code = "WORKSPACE_NOT_ALLOWED"


class IdentityNotLinkedError(DomainError):
    error_code = "IDENTITY_NOT_LINKED"


class PromApiUnavailableError(DomainError):
    error_code = "PROM_API_UNAVAILABLE"


class PromCredentialInvalidError(DomainError):
    """Raised when a delegated token is rejected by Prom (401/403) or does not belong to the
    expected prom_user_id.
    """

    error_code = "PROM_PERMISSION_DENIED"


class TenantIsolationError(DomainError):
    """Raised when an operation would cross a customer/tenant boundary (e.g. linking a channel
    installation from one customer to a Prom instance owned by another). Not part of the master
    prompt's original error catalog — added here because tenant-isolation rejection needs an
    explicit, auditable error distinct from a generic permission error. Documented here as the
    canonical addition.
    """

    error_code = "TENANT_ISOLATION_VIOLATION"


class NotFoundError(DomainError):
    """Raised when a referenced entity (installation, instance, identity, ...) does not exist."""

    error_code = "NOT_FOUND"


class IdentityAlreadyLinkedError(DomainError):
    """Raised when consuming a linking code would create a second *active* LinkedIdentity for the
    same channel_user_id or the same prom_user_id on an installation that already has one — the
    persistence layer's partial unique indexes (`uq_linked_identities_active_channel_user`,
    `uq_linked_identities_active_prom_user`) are the source of truth; this error is what an
    `IntegrityError` from those constraints gets translated into at the repository boundary. Not
    part of the master prompt's original error catalog — added for the same reason as
    TenantIsolationError/ChannelCredentialInvalidError.
    """

    error_code = "IDENTITY_ALREADY_LINKED"


class ChannelCredentialInvalidError(DomainError):
    """Raised when LINE/Discord credentials submitted for a new channel installation fail
    validation against the channel's own API. Not part of the master prompt's original error
    catalog — added here for the same reason as TenantIsolationError: an explicit, auditable
    error distinct from a generic validation error.
    """

    error_code = "CHANNEL_CREDENTIAL_INVALID"


class WorkItemNotFoundError(DomainError):
    """Prom returned 404 for a work item lookup/update. Part of the master prompt's original
    error catalog (`WORK_ITEM_NOT_FOUND`)."""

    error_code = "WORK_ITEM_NOT_FOUND"


class PromPermissionDeniedError(DomainError):
    """Prom rejected a work-item operation with 401/403 — the delegated token is valid (unlike
    PromCredentialInvalidError, which is about the token itself being bad/expired at linking
    time) but the user doesn't have permission for this specific action, e.g. they're not a
    member of the project, or Prom's RBAC denies the write. Same error_code as
    PromCredentialInvalidError (`PROM_PERMISSION_DENIED`, the master prompt's original catalog
    only defines one code for both origins) — kept as a distinct exception class so the two
    situations remain separable in code/tests even though they map to the same HTTP status.
    """

    error_code = "PROM_PERMISSION_DENIED"


class PromRequestInvalidError(DomainError):
    """Prom rejected a request with 400 (validation error on Prom's side, e.g. a bad state_id),
    or Hub itself detected a request-shape problem before even calling Prom (e.g. a project has
    no state with group == "completed", so MarkComplete has no valid target state to PATCH to —
    folded into this error rather than a bespoke NoCompletedStateError, since both cases are
    "the request cannot be fulfilled against Prom's actual state" rather than a distinct,
    independently-actionable failure mode). Not part of the master prompt's original catalog —
    documented addition, same pattern as TenantIsolationError et al.
    """

    error_code = "PROM_REQUEST_INVALID"


class ChannelDeliveryFailedError(DomainError):
    """A LINE/Discord API call (reply, push, or webhook-triggered send) failed — network error or
    a non-2xx response from the channel platform. Part of the master prompt's original error
    catalog (`CHANNEL_DELIVERY_FAILED`)."""

    error_code = "CHANNEL_DELIVERY_FAILED"


class InstallationRevokedError(DomainError):
    """Raised when `ActivateInstallation`/`DeactivateInstallation` (Gate 8, reversible
    active<->inactive toggles) is attempted on an installation whose status is `revoked` — revoke
    (`RevokeInstallation`) is a one-way action, deliberately not reachable from these two. Not
    part of the master prompt's original catalog — documented addition, same pattern as the
    errors above.
    """

    error_code = "INSTALLATION_REVOKED"


class IdempotencyKeyConflictError(DomainError):
    """Raised when a caller reuses an `Idempotency-Key` header with a request body that hashes
    differently from the one originally stored under that key — the key is meant to identify one
    logical request, not be reused across different ones. Not part of the master prompt's
    original catalog — documented addition, same pattern as the errors above.
    """

    error_code = "IDEMPOTENCY_KEY_CONFLICT"


class InstallationNotRevokedError(DomainError):
    """Raised when `DeleteChannelInstallation` is attempted on an installation whose status isn't
    `revoked` yet — deletion is a hard, irreversible removal (unlike revoke, which just flips a
    status flag and is fully auditable/reversible-in-spirit), so it's only reachable from the
    terminal `revoked` state, mirroring the one-way-ness `InstallationRevokedError` already
    enforces the other direction. Not part of the master prompt's original catalog — documented
    addition, same pattern as the errors above.
    """

    error_code = "INSTALLATION_NOT_REVOKED"


class InstallationHasLinkedIdentitiesError(DomainError):
    """Raised when `DeleteChannelInstallation` is attempted on an installation that has ever had a
    real user link to it (any `LinkedIdentity` row, active or unlinked) — that's real account-
    linking history, not the accidental/never-used installation this action exists to clean up.
    The admin must be shown this distinction rather than silently losing it. Not part of the
    master prompt's original catalog — documented addition, same pattern as the errors above.
    """

    error_code = "INSTALLATION_HAS_LINKED_IDENTITIES"
