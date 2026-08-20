"""WorkspaceConnection — a Prom/Plane workspace known to the Hub, scoped to one customer and
one PromInstance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class WorkspaceConnection:
    id: UUID
    customer_id: UUID
    prom_instance_id: UUID
    prom_workspace_id: str
    prom_workspace_slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    webhook_secret_encrypted: bytes | None = None
    """Gate 7: the Plane workspace's Webhook.secret_key, encrypted at rest (SecretProvider, same
    as every other credential in this codebase). Nullable — a WorkspaceConnection can exist
    before its Plane-side webhook is configured. Plane's webhook CRUD only exists in the
    session-authenticated `plane.app` (internal) API, not the public `plane.api` (X-Api-Key)
    REST API Hub uses everywhere else (verified: zero webhook endpoints under
    `apps/api/plane/api/`) — so registering the webhook is a manual, one-time admin action (log
    into Plane's web UI, create it, paste the secret here via
    `PATCH /api/v1/workspace-connections/{id}/webhook-secret`), the same pattern as LINE/Discord
    credentials, not a new one."""
