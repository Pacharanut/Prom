"""Presentation-layer request/response models. Pydantic is fine here (this is apps/api, not
domain/application) — these translate to/from the framework-free dataclasses in src/domain.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from src.domain.audit.entities import ResultStatus
from src.domain.customers.entities import CustomerStatus
from src.domain.identities.entities import IdentityStatus
from src.domain.instances.entities import InstanceStatus
from src.domain.integrations.entities import ChannelType, InstallationStatus


class CustomerCreateRequest(BaseModel):
    name: str


class CustomerResponse(BaseModel):
    id: UUID
    name: str
    status: CustomerStatus
    created_at: datetime
    updated_at: datetime


class PromInstanceCreateRequest(BaseModel):
    customer_id: UUID
    name: str
    base_url: str


class PromInstanceResponse(BaseModel):
    id: UUID
    customer_id: UUID
    name: str
    base_url: str
    status: InstanceStatus
    created_at: datetime
    updated_at: datetime


class WorkspaceConnectionCreateRequest(BaseModel):
    customer_id: UUID
    prom_instance_id: UUID
    prom_workspace_id: str
    prom_workspace_slug: str
    name: str


class WorkspaceConnectionResponse(BaseModel):
    id: UUID
    customer_id: UUID
    prom_instance_id: UUID
    prom_workspace_id: str
    prom_workspace_slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    has_webhook_secret: bool
    """Never echoes the secret itself back (see security requirement: "แสดง Secret ใน UI ได้เฉพาะ
    ตอนกรอก ห้ามส่งค่าจริงกลับหลังบันทึก") — just whether one has been configured."""
    webhook_url: str
    """The receiving URL to paste into Plane's own Webhook settings page (Plane's webhook CRUD
    only exists in its session-authenticated internal UI, not the public API Hub uses everywhere
    else — see WorkspaceConnection.webhook_secret_encrypted's docstring) — computed from
    settings.hub_public_base_url + this workspace_connection's id, not stored."""


class WebhookSecretSetRequest(BaseModel):
    secret: str


class ChannelInstallationCreateRequest(BaseModel):
    customer_id: UUID
    channel_type: ChannelType
    display_name: str
    credentials: dict[str, str]


class ChannelInstallationResponse(BaseModel):
    """Never includes encrypted_credentials — secrets are never returned after save, per the
    project's security requirements."""

    id: UUID
    customer_id: UUID
    channel_type: ChannelType
    display_name: str
    status: InstallationStatus
    external_account_id: str
    default_workspace_connection_id: UUID | None
    created_at: datetime
    updated_at: datetime


class InstallationWorkspaceLinkRequest(BaseModel):
    workspace_connection_id: UUID
    is_default: bool = False


class InstallationWorkspaceLinkResponse(BaseModel):
    id: UUID
    channel_installation_id: UUID
    workspace_connection_id: UUID
    created_at: datetime


class ChannelInstallationDetailResponse(ChannelInstallationResponse):
    """Gate 8: `GET /api/v1/installations/{id}` — the list-page shape plus its workspace
    mappings, for the "Integrations" admin detail view."""

    workspaces: list[InstallationWorkspaceLinkResponse]


class TestConnectionResponse(BaseModel):
    is_valid: bool
    error_message: str | None


class AuditLogEntryResponse(BaseModel):
    id: UUID
    customer_id: UUID | None
    prom_instance_id: UUID | None
    installation_id: UUID | None
    channel_type: ChannelType | None
    channel_user_id: str | None
    prom_user_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    correlation_id: str
    result_status: ResultStatus
    error_code: str | None
    occurred_at: datetime


class StartAccountLinkingRequest(BaseModel):
    prom_instance_id: UUID
    prom_user_id: str
    channel_installation_id: UUID
    delegated_token: str
    default_workspace_connection_id: UUID | None = None


class StartAccountLinkingResponse(BaseModel):
    """Returned only to a trusted server-side caller (Prom Core), which is what displays the
    code to the end user — never sent directly to a browser/LINE/Discord client."""

    code: str
    expires_at: datetime


class ConsumeLinkingCodeRequest(BaseModel):
    channel_installation_id: UUID
    channel_type: ChannelType
    channel_user_id: str
    submitted_code: str


class LinkedIdentityResponse(BaseModel):
    id: UUID
    customer_id: UUID
    channel_installation_id: UUID
    channel_type: ChannelType
    channel_user_id: str
    prom_instance_id: UUID
    prom_user_id: str
    default_workspace_connection_id: UUID | None
    status: IdentityStatus
    linked_at: datetime
    unlinked_at: datetime | None


class ErrorResponse(BaseModel):
    error_code: str
    message: str


# --- Work items (Gate 4) -----------------------------------------------------------------
# All work-item routes take `channel_installation_id` purely to resolve `customer_id` for the
# idempotency cache and audit log — the underlying Prom operation only needs prom_instance_id/
# prom_user_id/workspace_slug/project_id, consistent with the account-linking routes always
# deriving customer_id from a verified installation/instance rather than trusting a bare field.


class WorkItemResponse(BaseModel):
    id: str
    sequence_id: int
    project_id: str
    name: str
    state_id: str | None
    priority: str | None
    assignee_ids: list[str]
    target_date: date | None
    completed_at: datetime | None


class CreateWorkItemRequest(BaseModel):
    channel_installation_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    workspace_slug: str
    project_id: str
    title: str
    state_id: str | None = None
    assignee_ids: list[str] | None = None
    due_date: date | None = None
    description_html: str | None = None
    idempotency_key: str | None = None


class CreateWorkItemResponse(BaseModel):
    work_item: WorkItemResponse
    was_replay: bool


class MyTasksResponse(BaseModel):
    items: list[WorkItemResponse]
    truncated: bool


class UpdateStateRequest(BaseModel):
    channel_installation_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    workspace_slug: str
    project_id: str
    state_id: str


class ChangeAssigneeRequest(BaseModel):
    channel_installation_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    workspace_slug: str
    project_id: str
    assignee_ids: list[str]


class AddCommentRequest(BaseModel):
    channel_installation_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    workspace_slug: str
    project_id: str
    comment_html: str


class AddCommentResponse(BaseModel):
    comment_id: str


class CompleteWorkItemRequest(BaseModel):
    channel_installation_id: UUID
    prom_instance_id: UUID
    prom_user_id: str
    workspace_slug: str
    project_id: str


class WorkItemSearchResultResponse(BaseModel):
    id: str
    name: str
    sequence_id: int
    project_id: str
    project_identifier: str
