"""Notification use cases. Gate 5 only needs enough of this to (a) be directly exercisable by a
test and (b) be callable when Gate 7 wires Prom-webhook-driven notifications up to this. It is
NOT used by the LINE webhook handler's own replies/pushes within the same request — those already
have decrypted credentials in hand from processing the inbound webhook, so re-fetching and
re-decrypting the same installation via this use case would be a redundant DB round-trip for no
benefit. This use case is for the case where nothing about a channel session is already loaded —
e.g. a background Celery task reacting to a Prom webhook event (Gate 7).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NoReturn
from uuid import UUID, uuid4

from src.application.account_linking.ports import (
    AuditLogRepository,
    ChannelInstallationRepository,
    Clock,
    DelegatedCredentialRepository,
    LinkedIdentityRepository,
    PromInstanceRepository,
    SecretProvider,
    WorkspaceConnectionRepository,
)
from src.application.notifications.ports import (
    MessagePublisher,
    NotificationPreferenceRepository,
    OutboundMessageRepository,
)
from src.application.prom_api.ports import PromApiClient
from src.application.shared.audit_writer import AuditWriter
from src.application.webhooks.ports import WebhookDedupStore
from src.application.work_items.use_cases import ListMyWorkItems, ListProjects
from src.domain.audit.entities import ResultStatus
from src.domain.identities.entities import LinkedIdentity
from src.domain.integrations.entities import InstallationStatus
from src.domain.notifications.entities import (
    NotificationEventType,
    OutboundMessageRecord,
    OutboundMessageStatus,
)
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import (
    ChannelDeliveryFailedError,
    DomainError,
    IntegrationInactiveError,
    NotFoundError,
)

_DEBOUNCE_WINDOW_SECONDS = 45
"""Gate 7: how long ProcessPromWebhookEvent buffers same-(identity, work_item) events before
sending one summarized message. 45s — long enough to catch near-simultaneous field changes within
one edit (Plane's model_activity fires one webhook per changed field, so a multi-field PATCH
produces several rapid-fire deliveries), short enough that a DM notification still feels timely.
Named constant, not scattered magic numbers, per the same convention as
src/application/work_items/use_cases.py's _MAX_PROJECTS_PER_CALL etc."""

_DUE_DEDUP_TTL_SECONDS = 25 * 60 * 60
"""Slightly over 24h so a scheduler run that's a little late the next day doesn't get blocked by
yesterday's dedup key still being alive, while still guaranteeing at most one notification per
(identity, work_item, event_type) per calendar day."""


def _extract_assignee_ids(assignees_field: Any) -> list[str]:
    """`data.assignees` on an `event="issue"` webhook payload is a list of expanded user objects
    (IssueExpandSerializer's nested UserLiteSerializer — verified, Gate 0/7 discovery:
    `webhook_task.py:164`'s `context={"expand": ["labels", "assignees"]}`), each with an "id"
    key — not bare id strings. Defensively also accepts bare strings, in case a future Plane
    version or differently-configured expand context changes this."""
    if not assignees_field:
        return []
    ids: list[str] = []
    for entry in assignees_field:
        if isinstance(entry, dict):
            entry_id = entry.get("id")
            if entry_id:
                ids.append(str(entry_id))
        elif isinstance(entry, str):
            ids.append(entry)
    return ids


@dataclass
class _PendingNotification:
    prom_user_id: str
    event_type: NotificationEventType


def determine_notifications(
    payload: dict[str, Any],
) -> tuple[str | None, list[_PendingNotification]]:
    """Pure — no I/O, easily unit-tested standalone (see tests/application/test_notifications.py).
    Maps one verified Plane webhook payload to (work_item_id, notifications-to-buffer). Returns
    (None, []) for event types §8.6 defines no notification trigger for — project/module/cycle
    events, and issue create/update/delete actions with no notification-relevant `activity.field`
    — this is scope discipline, not a missed case: §8.6's trigger list is entirely work-item-centric.

    `action`, and `activity.field`'s exact values, were re-verified against a real running Plane
    instance's actual webhook deliveries (captured via ngrok, 2026-08-20) after the Gate 7/9
    "verified against webhook_task.py" claim turned out to only be true for the literal source
    text, not the values genuinely produced at runtime: `action` is `"created"`/`"updated"` (past
    tense — `webhook_activity`'s `verb`, passed straight through; the HTTP-method-keyed mapping
    dict in `webhook_send_task` never actually applies, since its input is always already one of
    these words, never "POST"/"PATCH"/etc), and field-changed activities use the underlying model
    field name, not the human relation name — `"state_id"` and `"assignee_ids"`, not `"state"`/
    `"assignees"` (`data.state`/`data.assignees` — the full nested objects — are unaffected, this
    is specifically about `activity.field`'s string value).

    `event="issue_comment"` is a special case: the comment payload doesn't carry the parent
    issue's assignees, so this function returns the notifications with `COMMENT` type already
    filled in as best it can from context available in the payload alone — the *caller*
    (ProcessPromWebhookEvent) is responsible for resolving actual assignees via a live Prom API
    lookup before this function's issue_comment branch is even reached; see there.
    """
    event = payload.get("event")
    action = payload.get("action")
    data = payload.get("data") or {}
    activity = payload.get("activity")

    if event == "issue":
        work_item_id = data.get("id")
        if not work_item_id:
            return None, []
        current_assignee_ids = _extract_assignee_ids(data.get("assignees"))

        if action == "created":
            return work_item_id, [
                _PendingNotification(uid, NotificationEventType.ASSIGNED)
                for uid in current_assignee_ids
            ]

        if action == "updated" and isinstance(activity, dict):
            field = activity.get("field")
            if field == "assignee_ids":
                old_ids = set(activity.get("old_value") or [])
                new_ids = set(activity.get("new_value") or [])
                notes = [
                    _PendingNotification(uid, NotificationEventType.ASSIGNED)
                    for uid in (new_ids - old_ids)
                ]
                notes += [
                    _PendingNotification(uid, NotificationEventType.UNASSIGNED)
                    for uid in (old_ids - new_ids)
                ]
                return work_item_id, notes
            if field == "state_id":
                state = data.get("state") or {}
                event_type = (
                    NotificationEventType.COMPLETED
                    if isinstance(state, dict) and state.get("group") == "completed"
                    else NotificationEventType.STATE_CHANGED
                )
                return work_item_id, [
                    _PendingNotification(uid, event_type) for uid in current_assignee_ids
                ]
        return work_item_id, []

    if event == "issue_comment" and action == "created":
        work_item_id = data.get("issue")
        return (str(work_item_id) if work_item_id else None), []

    return None, []


@dataclass
class SendPrivateNotification:
    installations: ChannelInstallationRepository
    secrets_provider: SecretProvider
    publisher: MessagePublisher
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self, *, linked_identity: LinkedIdentity, text: str, correlation_id: str
    ) -> None:
        audit = AuditWriter(self.audit_log, self.clock)

        async def _fail(error: DomainError) -> NoReturn:
            await audit.write(
                customer_id=linked_identity.customer_id,
                prom_instance_id=linked_identity.prom_instance_id,
                installation_id=linked_identity.channel_installation_id,
                channel_type=linked_identity.channel_type,
                channel_user_id=linked_identity.channel_user_id,
                prom_user_id=linked_identity.prom_user_id,
                action="notification.failed",
                target_type="linked_identity",
                target_id=str(linked_identity.id),
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        installation = await self.installations.get(linked_identity.channel_installation_id)
        if installation is None:
            await _fail(
                NotFoundError(f"installation {linked_identity.channel_installation_id} not found")
            )
        if installation.status is not InstallationStatus.ACTIVE:
            await _fail(IntegrationInactiveError(f"installation {installation.id} is not active"))

        credentials: dict[str, str] = json.loads(
            self.secrets_provider.decrypt(installation.encrypted_credentials).decode("utf-8")
        )

        try:
            await self.publisher.send_private_message(
                channel_type=linked_identity.channel_type,
                credentials=credentials,
                channel_user_id=linked_identity.channel_user_id,
                text=text,
            )
        except ChannelDeliveryFailedError as exc:
            await _fail(exc)

        await audit.write(
            customer_id=linked_identity.customer_id,
            prom_instance_id=linked_identity.prom_instance_id,
            installation_id=linked_identity.channel_installation_id,
            channel_type=linked_identity.channel_type,
            channel_user_id=linked_identity.channel_user_id,
            prom_user_id=linked_identity.prom_user_id,
            action="notification.delivered",
            target_type="linked_identity",
            target_id=str(linked_identity.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )


@dataclass
class ProcessPromWebhookEvent:
    """Gate 7: given one already-signature-verified, already-deduplicated Prom webhook payload,
    determine who should be notified and buffer each into the debounce window (see
    _DEBOUNCE_WINDOW_SECONDS). Does NOT send anything itself — FlushOutboundMessage does, once the
    Celery countdown this schedules fires. Framework-free: `schedule_send` is injected as a plain
    callable rather than importing Celery here, matching this codebase's rule that application
    code never imports celery/fastapi/sqlalchemy/httpx directly (see AGENTS.md); the real
    `lambda record_id: send_debounced_notification.apply_async(...)` is supplied by
    src/infrastructure/messaging/notification_tasks.py, the one layer allowed to import Celery.
    """

    workspace_connections: WorkspaceConnectionRepository
    linked_identities: LinkedIdentityRepository
    notification_preferences: NotificationPreferenceRepository
    outbound_messages: OutboundMessageRepository
    delegated_credentials: DelegatedCredentialRepository
    prom_instances: PromInstanceRepository
    secrets_provider: SecretProvider
    prom_api_client: PromApiClient
    clock: Clock
    schedule_send: Callable[[UUID], None]
    """Enqueues the debounced-send Celery task, given the new OutboundMessageRecord's id — a
    plain synchronous callable (Celery's own `apply_async` is itself synchronous, it just enqueues
    to the broker) injected so this module never imports celery directly (framework-free rule).
    The real implementation lives in src/infrastructure/messaging/notification_tasks.py."""

    async def execute(
        self, *, workspace_connection_id: UUID, payload: dict[str, Any], correlation_id: str
    ) -> None:
        workspace_connection = await self.workspace_connections.get(workspace_connection_id)
        if workspace_connection is None:
            return

        work_item_id, notifications = determine_notifications(payload)
        if work_item_id is None:
            return

        if payload.get("event") == "issue_comment":
            notifications = await self._resolve_comment_assignees(workspace_connection, payload)

        if not notifications:
            return

        for note in notifications:
            identities = await self.linked_identities.list_active_by_prom_user(
                workspace_connection.prom_instance_id, note.prom_user_id
            )
            for identity in identities:
                if not await self.notification_preferences.is_enabled(identity.id, note.event_type):
                    continue
                await self._buffer(identity.id, work_item_id, note.event_type)

    async def _resolve_comment_assignees(
        self, workspace_connection: WorkspaceConnection, payload: dict[str, Any]
    ) -> list[_PendingNotification]:
        """See DelegatedCredentialRepository.get_any_for_instance's docstring for why this needs
        *any* credential rather than a specific user's — we don't know who to notify yet, that's
        exactly what this lookup determines."""
        data = payload.get("data") or {}
        project_id = data.get("project")
        if not project_id:
            return []

        credential = await self.delegated_credentials.get_any_for_instance(
            workspace_connection.prom_instance_id
        )
        if credential is None:
            return []  # no one linked yet on this instance — documented limitation, not a crash

        instance = await self.prom_instances.get(workspace_connection.prom_instance_id)
        if instance is None:
            return []

        token = self.secrets_provider.decrypt(credential.encrypted_token).decode("utf-8")
        work_item_id = data.get("issue")
        try:
            work_item = await self.prom_api_client.get_work_item(
                instance.base_url,
                token,
                workspace_connection.prom_workspace_slug,
                str(project_id),
                str(work_item_id),
            )
        except DomainError:
            return []

        return [
            _PendingNotification(uid, NotificationEventType.COMMENT)
            for uid in work_item.assignee_ids
        ]

    async def _buffer(
        self, linked_identity_id: UUID, work_item_id: str, event_type: NotificationEventType
    ) -> None:
        existing = await self.outbound_messages.get_pending(linked_identity_id, work_item_id)
        now = self.clock.now()
        if existing is not None:
            if event_type not in existing.event_types_batched:
                existing.event_types_batched.append(event_type)
                await self.outbound_messages.save(existing)
            return

        record = OutboundMessageRecord(
            id=uuid4(),
            linked_identity_id=linked_identity_id,
            work_item_id=work_item_id,
            event_types_batched=[event_type],
            status=OutboundMessageStatus.PENDING,
            attempt_count=0,
            scheduled_for=now + timedelta(seconds=_DEBOUNCE_WINDOW_SECONDS),
            sent_at=None,
            created_at=now,
        )
        await self.outbound_messages.add(record)
        self.schedule_send(record.id)


_EVENT_TYPE_LABELS: dict[NotificationEventType, str] = {
    NotificationEventType.ASSIGNED: "คุณถูกมอบหมายงานนี้",
    NotificationEventType.UNASSIGNED: "คุณถูกถอดออกจากงานนี้",
    NotificationEventType.STATE_CHANGED: "สถานะงานเปลี่ยนแปลง",
    NotificationEventType.COMMENT: "มีความคิดเห็นใหม่",
    NotificationEventType.COMPLETED: "งานนี้เสร็จสมบูรณ์แล้ว",
    NotificationEventType.DUE_SOON: "งานนี้ใกล้ถึงกำหนดส่งพรุ่งนี้",
    NotificationEventType.DUE_TODAY: "งานนี้ครบกำหนดส่งวันนี้",
    NotificationEventType.OVERDUE: "งานนี้เลยกำหนดส่งแล้ว",
}


@dataclass
class FlushOutboundMessage:
    """Gate 7: fires when the Celery countdown ProcessPromWebhookEvent scheduled elapses. Composes
    one summarized message from whatever event types got batched into the debounce window and
    sends it via the existing SendPrivateNotification (Gate 5). Message text uses the raw Prom
    work_item_id (not a nicer "{project_identifier}-{sequence_id}: {title}" summary) — composing
    that would need another delegated-credential lookup + GetWorkItem call, and unlike
    ProcessPromWebhookEvent's comment-assignee resolution (which has no other way to determine
    recipients), this is purely cosmetic; deferred, see README "Known limitations"."""

    outbound_messages: OutboundMessageRepository
    linked_identities: LinkedIdentityRepository
    installations: ChannelInstallationRepository
    secrets_provider: SecretProvider
    publisher: MessagePublisher
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(self, *, record_id: UUID, correlation_id: str) -> None:
        record = await self.outbound_messages.get(record_id)
        if record is None or record.status is not OutboundMessageStatus.PENDING:
            return  # already sent/dead-lettered by an earlier attempt, or a stray call

        identity = await self.linked_identities.get(record.linked_identity_id)
        if identity is None:
            return

        text = self._compose(record)
        sender = SendPrivateNotification(
            installations=self.installations,
            secrets_provider=self.secrets_provider,
            publisher=self.publisher,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        try:
            await sender.execute(linked_identity=identity, text=text, correlation_id=correlation_id)
        except ChannelDeliveryFailedError:
            record.attempt_count += 1
            await self.outbound_messages.save(record)
            raise  # Celery's autoretry_for handles backoff; the task dead-letters after max_retries

        record.status = OutboundMessageStatus.SENT
        record.sent_at = self.clock.now()
        await self.outbound_messages.save(record)

    def _compose(self, record: OutboundMessageRecord) -> str:
        lines = [_EVENT_TYPE_LABELS.get(t, t.value) for t in record.event_types_batched]
        return f"งาน {record.work_item_id}: " + " · ".join(lines)


@dataclass
class CheckDueDateNotifications:
    """Gate 7 scheduled task body (invoked by Celery beat, see
    src/infrastructure/messaging/notification_tasks.py) — reuses Gate 4's ListProjects/
    ListMyWorkItems as-is (same caps, same client) rather than any new work-item-reading logic.
    Sends directly via SendPrivateNotification (no debounce buffer — a scheduler run naturally
    produces at most one due_today/overdue/due_soon event per work item per run, nothing to
    collapse), gated by a Redis dedup key so multiple beat runs in one day (this task runs hourly;
    see notification_tasks.py) don't repeat the same notification — see _DUE_DEDUP_TTL_SECONDS.
    """

    linked_identities: LinkedIdentityRepository
    workspace_connections: WorkspaceConnectionRepository
    list_projects: ListProjects
    list_my_work_items: ListMyWorkItems
    notification_preferences: NotificationPreferenceRepository
    installations: ChannelInstallationRepository
    secrets_provider: SecretProvider
    publisher: MessagePublisher
    audit_log: AuditLogRepository
    clock: Clock
    dedup_store: WebhookDedupStore
    """Reused as-is rather than adding a second near-identical port — same
    `.mark_seen_if_new(key, ttl_seconds) -> bool` shape LINE/Discord webhook dedup already uses,
    just with a different key namespace (`due-notify:...` vs `webhook-seen:...`)."""

    async def execute(self, *, correlation_id: str) -> None:
        identities = await self.linked_identities.list_all_active_with_default_workspace()
        sender = SendPrivateNotification(
            installations=self.installations,
            secrets_provider=self.secrets_provider,
            publisher=self.publisher,
            audit_log=self.audit_log,
            clock=self.clock,
        )
        today = self.clock.now().date().isoformat()

        for identity in identities:
            assert identity.default_workspace_connection_id is not None
            workspace = await self.workspace_connections.get(
                identity.default_workspace_connection_id
            )
            if workspace is None:
                continue

            try:
                projects = await self.list_projects.execute(
                    prom_instance_id=identity.prom_instance_id,
                    prom_user_id=identity.prom_user_id,
                    workspace_slug=workspace.prom_workspace_slug,
                    correlation_id=correlation_id,
                )
            except DomainError:
                continue
            project_ids = [p.id for p in projects]

            for view, event_type in (
                ("due_soon", NotificationEventType.DUE_SOON),
                ("due_today", NotificationEventType.DUE_TODAY),
                ("overdue", NotificationEventType.OVERDUE),
            ):
                if not await self.notification_preferences.is_enabled(identity.id, event_type):
                    continue
                try:
                    result = await self.list_my_work_items.execute(
                        prom_instance_id=identity.prom_instance_id,
                        prom_user_id=identity.prom_user_id,
                        workspace_slug=workspace.prom_workspace_slug,
                        project_ids=project_ids,
                        view=view,  # type: ignore[arg-type]
                        correlation_id=correlation_id,
                    )
                except DomainError:
                    continue

                for item in result.items:
                    key = f"due-notify:{identity.id}:{item.id}:{event_type.value}:{today}"
                    is_new = await self.dedup_store.mark_seen_if_new(key, _DUE_DEDUP_TTL_SECONDS)
                    if not is_new:
                        continue
                    try:
                        await sender.execute(
                            linked_identity=identity,
                            text=f"งาน {item.name}: {_EVENT_TYPE_LABELS[event_type]}",
                            correlation_id=correlation_id,
                        )
                    except DomainError:
                        continue
