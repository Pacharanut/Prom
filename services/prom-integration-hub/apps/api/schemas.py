"""Presentation-layer request/response models. Pydantic is fine here (this is apps/api, not
domain/application) — these translate to/from the framework-free dataclasses in src/domain.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from src.domain.audit.entities import ResultStatus
from src.domain.customers.entities import CustomerStatus
from src.domain.identities.entities import IdentityStatus
from src.domain.instances.entities import InstanceStatus
from src.domain.integrations.entities import ChannelType, InstallationStatus
from src.domain.notifications.entities import NotificationEventType


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


# --- AI settings (Phase 2) --------------------------------------------------------------------


class AiSettingsResponse(BaseModel):
    """Never contains the API key itself — only whether one is set and where it comes from."""

    customer_id: UUID
    is_enabled: bool
    base_url: str | None
    model: str
    timeout_seconds: int
    has_api_key: bool
    api_key_source: Literal["custom", "environment"] | None
    is_customized: bool


class AiSettingsUpdateRequest(BaseModel):
    is_enabled: bool
    base_url: str
    model: str
    timeout_seconds: int = 15
    api_key: str | None = None
    """Omit (or null) to keep the stored key; a non-empty value replaces it."""
    clear_api_key: bool = False
    """True removes the stored key so the Hub's NT_AI_HUB_KEY default applies again."""


class AiSettingsTestRequest(BaseModel):
    """Every field optional: omitted ones fall back to the saved settings / Hub defaults, so the
    settings page can test edits before saving them."""

    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int | None = None
    api_key: str | None = None


class AiConnectionTestResponse(BaseModel):
    is_ok: bool
    models: list[str]
    model_found: bool
    latency_ms: int | None
    sample_intent: str | None
    error_code: str | None
    error_message: str | None


# --- Notification preferences (Phase 2B) ------------------------------------------------------


class NotificationPreferenceItem(BaseModel):
    event_type: NotificationEventType
    enabled: bool


class NotificationPreferencesResponse(BaseModel):
    linked_identity_id: UUID
    preferences: list[NotificationPreferenceItem]


class NotificationPreferencesUpdateRequest(BaseModel):
    """Only the listed event types change; omitted ones keep their current setting."""

    preferences: dict[NotificationEventType, bool]


# --- Auto-tagging (Phase 2C) ------------------------------------------------------------------


class ClassificationRuleItem(BaseModel):
    keyword: str
    label_name: str | None = None
    priority: Literal["urgent", "high", "medium", "low", "none"] | None = None


class ClassificationSettingsResponse(BaseModel):
    workspace_connection_id: UUID
    is_enabled: bool
    use_ai_fallback: bool
    rules: list[ClassificationRuleItem]
    is_customized: bool


class ClassificationSettingsUpdateRequest(BaseModel):
    """Replaces the whole rule list (the settings page edits it as one table)."""

    is_enabled: bool = True
    use_ai_fallback: bool = True
    rules: list[ClassificationRuleItem] = []


# --- Timeline (Phase 2E) ----------------------------------------------------------------------


class TimelineEntryResponse(BaseModel):
    at: datetime
    source: Literal["line", "discord", "hub", "plane"]
    kind: str
    actor_id: str | None
    actor_name: str | None
    old_value: str | None
    new_value: str | None
