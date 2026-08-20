"""WorkspaceConnection creation + Gate 7's webhook-secret admin endpoint — see routers/
customers.py for the create-endpoint rationale."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from apps.api.dependencies import (
    get_register_workspace_connection,
    get_workspace_connection_repository,
    require_service_token,
)
from apps.api.schemas import (
    WebhookSecretSetRequest,
    WorkspaceConnectionCreateRequest,
    WorkspaceConnectionResponse,
)
from src.adapters.clock import SystemClock
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.application.account_linking.use_cases import RegisterWorkspaceConnection
from src.domain.workspaces.entities import WorkspaceConnection
from src.infrastructure.configuration.settings import get_settings
from src.infrastructure.observability.logging import get_correlation_id

router = APIRouter(
    prefix="/api/v1/workspace-connections",
    tags=["workspace-connections"],
    dependencies=[Depends(require_service_token)],
)


def _webhook_url(workspace_connection_id: UUID) -> str:
    """The receiving URL to paste into Plane's own Webhook settings page — see
    WorkspaceConnection.webhook_secret_encrypted's docstring for why this is a manual admin step
    rather than something Hub can self-provision via the public Prom API."""
    base = get_settings().hub_public_base_url.rstrip("/")
    return f"{base}/webhooks/v1/prom/{workspace_connection_id}"


def _to_response(workspace: WorkspaceConnection) -> WorkspaceConnectionResponse:
    return WorkspaceConnectionResponse(
        id=workspace.id,
        customer_id=workspace.customer_id,
        prom_instance_id=workspace.prom_instance_id,
        prom_workspace_id=workspace.prom_workspace_id,
        prom_workspace_slug=workspace.prom_workspace_slug,
        name=workspace.name,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        has_webhook_secret=workspace.webhook_secret_encrypted is not None,
        webhook_url=_webhook_url(workspace.id),
    )


@router.post("", response_model=WorkspaceConnectionResponse)
async def create_workspace_connection(
    body: WorkspaceConnectionCreateRequest,
    response: Response,
    use_case: Annotated[RegisterWorkspaceConnection, Depends(get_register_workspace_connection)],
) -> WorkspaceConnectionResponse:
    """Get-or-create: safe to call repeatedly for the same (prom_instance_id, prom_workspace_id)
    — e.g. every time an admin opens the Integrations page for a workspace. Returns 201 only when
    a new row was actually created, 200 when an existing one was found (and its name/slug
    refreshed to match this call) — see RegisterWorkspaceConnection's docstring."""
    workspace, was_created = await use_case.execute(
        customer_id=body.customer_id,
        prom_instance_id=body.prom_instance_id,
        prom_workspace_id=body.prom_workspace_id,
        prom_workspace_slug=body.prom_workspace_slug,
        name=body.name,
        correlation_id=get_correlation_id() or "unknown",
    )
    response.status_code = 201 if was_created else 200
    return _to_response(workspace)


@router.get("/{workspace_connection_id}", response_model=WorkspaceConnectionResponse)
async def get_workspace_connection(
    workspace_connection_id: UUID,
    repository: Annotated[
        PostgresWorkspaceConnectionRepository, Depends(get_workspace_connection_repository)
    ],
) -> WorkspaceConnectionResponse:
    workspace = await repository.get(workspace_connection_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace connection not found")
    return _to_response(workspace)


@router.patch(
    "/{workspace_connection_id}/webhook-secret", response_model=WorkspaceConnectionResponse
)
async def set_workspace_webhook_secret(
    workspace_connection_id: UUID,
    body: WebhookSecretSetRequest,
    repository: Annotated[
        PostgresWorkspaceConnectionRepository, Depends(get_workspace_connection_repository)
    ],
) -> WorkspaceConnectionResponse:
    """Admin action: paste in the `secret_key` Plane generated when an admin manually created the
    workspace's webhook in Plane's own settings UI (see WorkspaceConnection.webhook_secret_
    encrypted's docstring — Plane's webhook CRUD has no public-API equivalent Hub could call
    itself). Encrypted at rest, never echoed back — the response's `has_webhook_secret` just
    confirms one is now configured, per "แสดง Secret ใน UI ได้เฉพาะตอนกรอก ห้ามส่งค่าจริงกลับหลัง
    บันทึก"."""
    workspace = await repository.get(workspace_connection_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace connection not found")

    secret_provider = FernetSecretProvider(get_settings().secret_encryption_key)
    workspace.webhook_secret_encrypted = secret_provider.encrypt(body.secret.encode("utf-8"))
    workspace.updated_at = SystemClock().now()
    await repository.save(workspace)
    return _to_response(workspace)
