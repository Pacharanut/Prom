"""LineWebhookHandler — the controller for everything an inbound LINE webhook can trigger:
account linking (OTP), the main menu, the create-work-item flow, my-tasks, and the update flow.
Constructor-injected with application-layer ports/use-cases only (never a concrete adapter
directly) — the composition root (apps/api/dependencies.py) does all adapter wiring, same
Hexagonal pattern as every use case in this codebase. This keeps src/channels/line/ depending only
on src/application + src/domain, so the existing import-linter "layers" contract needs no change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from src.application.account_linking.ports import (
    ChannelInstallationRepository,
    DelegatedCredentialRepository,
    LinkedIdentityRepository,
    PromInstanceRepository,
    SecretProvider,
    WorkspaceConnectionRepository,
)
from src.application.account_linking.use_cases import ConsumeLinkingCode
from src.application.prom_api.ports import PromApiClient
from src.application.webhooks.ports import WebhookDedupStore
from src.application.work_items.conversation import (
    ConversationState,
    CreateFlowStep,
    CreateWorkItemFlow,
    FlowContext,
    InvalidConversationInputError,
    UpdateFlowStep,
    UpdateWorkItemFlow,
)
from src.application.work_items.ports import ConversationStateRepository
from src.application.work_items.use_cases import ListMyWorkItems, ListProjects
from src.channels.line import messages as msg
from src.channels.line.client import reply_message
from src.channels.line.events import LineEvent, parse_line_webhook
from src.channels.line.postback import decode_postback
from src.domain.identities.entities import LinkedIdentity
from src.domain.integrations.entities import ChannelInstallation, ChannelType
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import ChannelDeliveryFailedError, DomainError

_CONVERSATION_TTL_SECONDS = 600
"""10 minutes — same duration used as a reference point by the Plane_Line prototype's conversation
TTL (Gate 0 finding), reused here as a sensible default; not shared code."""
_WEBHOOK_DEDUP_TTL_SECONDS = 24 * 60 * 60

_MENU_KEYWORDS = {"เมนู", "สวัสดี", "help", "menu", "hi", "hello"}
_CANCEL_KEYWORDS = {"ยกเลิก", "cancel"}

_PendingAction = Literal["create", "my_tasks", "update"]


def _is_otp_candidate(text: str) -> bool:
    return text.strip().isdigit() and len(text.strip()) == 6


@dataclass
class LineWebhookHandler:
    linked_identities: LinkedIdentityRepository
    channel_installations: ChannelInstallationRepository
    workspace_connections: WorkspaceConnectionRepository
    delegated_credentials: DelegatedCredentialRepository
    prom_instances: PromInstanceRepository
    secrets_provider: SecretProvider
    prom_api_client: PromApiClient
    consume_linking_code: ConsumeLinkingCode
    dedup_store: WebhookDedupStore
    conversation_states: ConversationStateRepository
    create_flow: CreateWorkItemFlow
    update_flow: UpdateWorkItemFlow
    list_projects: ListProjects
    list_my_work_items: ListMyWorkItems

    async def handle_payload(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        raw_payload: dict[str, object],
    ) -> None:
        for event in parse_line_webhook(raw_payload):
            if event.event_type == "other" or event.user_id is None:
                continue
            is_new = await self.dedup_store.mark_seen_if_new(
                event.dedup_key(), ttl_seconds=_WEBHOOK_DEDUP_TTL_SECONDS
            )
            if not is_new:
                continue
            await self._handle_event(installation, channel_access_token, event)

    async def _reply(
        self, channel_access_token: str, reply_token: str | None, message: dict[str, object]
    ) -> None:
        if reply_token is None:
            return
        try:
            await reply_message(channel_access_token, reply_token, [message])
        except ChannelDeliveryFailedError:
            # Best-effort: the webhook route must still return 200 to LINE regardless (retrying a
            # delivery failure by having LINE redeliver the same event would just hit the dedup
            # store and produce no reply at all) — swallow here, nothing else to do.
            pass

    async def _handle_event(
        self, installation: ChannelInstallation, channel_access_token: str, event: LineEvent
    ) -> None:
        assert event.user_id is not None
        channel_user_id = event.user_id
        correlation_id = str(uuid4())

        identity = await self.linked_identities.get_active_by_channel_user(
            installation.id, channel_user_id
        )

        if event.event_type == "message" and event.text is not None:
            await self._handle_message(
                installation,
                channel_access_token,
                event,
                identity,
                event.text.strip(),
                correlation_id,
            )
            return

        if event.event_type == "postback" and event.postback_data is not None:
            if identity is None:
                await self._reply(
                    channel_access_token,
                    event.reply_token,
                    msg.build_error_message("IDENTITY_NOT_LINKED"),
                )
                return
            await self._handle_postback(
                installation, channel_access_token, event, identity, correlation_id
            )

    async def _handle_message(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        identity: LinkedIdentity | None,
        text: str,
        correlation_id: str,
    ) -> None:
        assert event.user_id is not None

        state = await self.conversation_states.get(installation.id, event.user_id)
        if state is not None and text.lower() in {t.lower() for t in _CANCEL_KEYWORDS}:
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._reply(
                channel_access_token, event.reply_token, msg.build_cancelled_message()
            )
            return

        if identity is None:
            if _is_otp_candidate(text):
                await self._handle_otp(
                    installation, channel_access_token, event, text, correlation_id
                )
            else:
                await self._reply(
                    channel_access_token,
                    event.reply_token,
                    msg.build_error_message("IDENTITY_NOT_LINKED"),
                )
            return

        if state is not None:
            await self._handle_flow_text_input(
                installation, channel_access_token, event, identity, state, text, correlation_id
            )
            return

        lowered = text.lower()
        if lowered in {t.lower() for t in _MENU_KEYWORDS}:
            await self._reply(channel_access_token, event.reply_token, msg.build_menu_message())
        elif text == "สร้างงาน":
            await self._start_or_defer(
                installation, channel_access_token, event, identity, "create"
            )
        elif text == "งานของฉัน":
            await self._start_or_defer(
                installation, channel_access_token, event, identity, "my_tasks"
            )
        elif text == "อัปเดต":
            await self._start_or_defer(
                installation, channel_access_token, event, identity, "update"
            )
        else:
            await self._reply(channel_access_token, event.reply_token, msg.build_menu_message())

    # --- Account linking -------------------------------------------------------------------------

    async def _handle_otp(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        code: str,
        correlation_id: str,
    ) -> None:
        assert event.user_id is not None
        try:
            identity = await self.consume_linking_code.execute(
                channel_installation_id=installation.id,
                channel_type=ChannelType.LINE,
                channel_user_id=event.user_id,
                submitted_code=code,
                correlation_id=correlation_id,
            )
        except DomainError as exc:
            await self._reply(
                channel_access_token, event.reply_token, msg.build_error_message(exc.error_code)
            )
            return

        instance = await self.prom_instances.get(identity.prom_instance_id)
        credential = await self.delegated_credentials.get(
            identity.prom_instance_id, identity.prom_user_id
        )
        display_profile = None
        if instance is not None and credential is not None:
            token = self.secrets_provider.decrypt(credential.encrypted_token).decode("utf-8")
            try:
                display_profile = await self.prom_api_client.get_current_user(
                    instance.base_url, token
                )
            except DomainError:
                display_profile = None

        workspaces = await self.channel_installations.list_workspaces(installation.id)
        if display_profile is not None:
            await self._reply(
                channel_access_token,
                event.reply_token,
                msg.build_linking_success_message(
                    display_profile, has_multiple_workspaces=len(workspaces) > 1
                ),
            )
        else:
            await self._reply(
                channel_access_token, event.reply_token, msg.build_cancelled_message()
            )

    # --- Workspace resolution --------------------------------------------------------------------

    async def _resolve_workspace(
        self, installation_id: UUID, default_workspace_connection_id: UUID | None
    ) -> WorkspaceConnection | list[WorkspaceConnection]:
        """Returns the resolved WorkspaceConnection, or a list of candidates (len != 1) the
        caller must handle: empty means no workspace is linked to this installation at all
        (configuration error), more than one means the user must be asked to pick."""
        if default_workspace_connection_id is not None:
            ws = await self.workspace_connections.get(default_workspace_connection_id)
            if ws is not None:
                return ws

        links = await self.channel_installations.list_workspaces(installation_id)
        candidates: list[WorkspaceConnection] = []
        for link in links:
            ws = await self.workspace_connections.get(link.workspace_connection_id)
            if ws is not None:
                candidates.append(ws)

        if len(candidates) == 1:
            return candidates[0]
        return candidates

    async def _start_or_defer(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        identity: LinkedIdentity,
        action: _PendingAction,
    ) -> None:
        assert event.user_id is not None
        resolved = await self._resolve_workspace(
            installation.id, identity.default_workspace_connection_id
        )

        if isinstance(resolved, list):
            if not resolved:
                await self._reply(
                    channel_access_token,
                    event.reply_token,
                    {"type": "text", "text": "บัญชีนี้ยังไม่ได้เชื่อมกับ Workspace ใดเลย ติดต่อผู้ดูแลระบบ"},
                )
                return
            state = ConversationState(
                flow="workspace_select", step="select", data={"pending_action": action}
            )
            await self.conversation_states.set(
                installation.id, event.user_id, state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(
                channel_access_token, event.reply_token, msg.build_workspace_picker(resolved)
            )
            return

        await self._begin_action(
            installation, channel_access_token, event, identity, resolved, action
        )

    async def _begin_action(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        identity: LinkedIdentity,
        workspace: WorkspaceConnection,
        action: _PendingAction,
    ) -> None:
        assert event.user_id is not None
        ctx = FlowContext(
            prom_instance_id=identity.prom_instance_id,
            prom_user_id=identity.prom_user_id,
            workspace_slug=workspace.prom_workspace_slug,
            correlation_id=str(uuid4()),
        )
        if action == "create":
            state = self.create_flow.start()
            state.data["workspace_connection_id"] = str(workspace.id)
            await self.conversation_states.set(
                installation.id, event.user_id, state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(channel_access_token, event.reply_token, msg.build_title_prompt())
        else:
            await self._show_my_tasks(installation, channel_access_token, event, ctx)

    async def _show_my_tasks(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        ctx: FlowContext,
    ) -> None:
        projects = await self.list_projects.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            correlation_id=ctx.correlation_id,
        )
        result = await self.list_my_work_items.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_ids=[p.id for p in projects],
            view="assigned",
            correlation_id=ctx.correlation_id,
        )
        projects_by_id = {p.id: p for p in projects}
        await self._reply(
            channel_access_token,
            event.reply_token,
            msg.build_my_tasks_list(result.items, projects_by_id, truncated=result.truncated),
        )

    # --- Free-text input mid-flow -----------------------------------------------------------------

    async def _handle_flow_text_input(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        identity: LinkedIdentity,
        state: ConversationState,
        text: str,
        correlation_id: str,
    ) -> None:
        assert event.user_id is not None
        ctx = await self._flow_context(identity, state, correlation_id)

        if state.flow == "create" and state.step == CreateFlowStep.TITLE:
            try:
                next_state, projects = await self.create_flow.submit_title(ctx, state, text)
            except InvalidConversationInputError:
                await self._reply(channel_access_token, event.reply_token, msg.build_title_prompt())
                return
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(
                channel_access_token, event.reply_token, msg.build_project_picker(projects)
            )
            return

        if state.flow == "create" and state.step == CreateFlowStep.DUE_DATE:
            try:
                next_state = self.create_flow.submit_due_date_text(state, text)
            except InvalidConversationInputError:
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_due_date_prompt()
                )
                return
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(
                channel_access_token, event.reply_token, await self._review_card(ctx, next_state)
            )
            return

        if state.flow == "update" and state.step == UpdateFlowStep.AWAIT_COMMENT:
            await self.update_flow.confirm_add_comment(ctx, state, text)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._reply(
                channel_access_token,
                event.reply_token,
                msg.build_update_success_message(action="comment"),
            )
            return

        # Any other step expects a button tap, not free text — a short nudge is enough; we don't
        # attempt to redraw the exact picker without more state than is worth carrying.
        await self._reply(
            channel_access_token,
            event.reply_token,
            {"type": "text", "text": "กรุณาเลือกจากตัวเลือกด้านบน หรือพิมพ์ 'ยกเลิก'"},
        )

    async def _review_card(self, ctx: FlowContext, state: ConversationState) -> dict[str, object]:
        project_id = state.data["project_id"]
        projects = await self.list_projects.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            correlation_id=ctx.correlation_id,
        )
        project = next((p for p in projects if p.id == project_id), None)

        state_name = None
        if state.data.get("state_id"):
            states = await self.create_flow.list_states.execute(
                prom_instance_id=ctx.prom_instance_id,
                prom_user_id=ctx.prom_user_id,
                workspace_slug=ctx.workspace_slug,
                project_id=project_id,
                correlation_id=ctx.correlation_id,
            )
            found_state = next((s for s in states if s.id == state.data["state_id"]), None)
            state_name = found_state.name if found_state else None

        assignee_name = None
        if state.data.get("assignee_id"):
            members = await self.create_flow.list_project_members.execute(
                prom_instance_id=ctx.prom_instance_id,
                prom_user_id=ctx.prom_user_id,
                workspace_slug=ctx.workspace_slug,
                project_id=project_id,
                correlation_id=ctx.correlation_id,
            )
            found_member = next(
                (m for m in members if m.user_id == state.data["assignee_id"]), None
            )
            assignee_name = found_member.display_name if found_member else None

        return msg.build_review_card(
            title=state.data["title"],
            project_name=project.name if project else project_id,
            state_name=state_name,
            assignee_name=assignee_name,
            due_date=state.data.get("due_date"),
        )

    async def _flow_context(
        self, identity: LinkedIdentity, state: ConversationState, correlation_id: str
    ) -> FlowContext:
        workspace_connection_id = state.data.get("workspace_connection_id")
        ws: WorkspaceConnection | None
        if workspace_connection_id is not None:
            ws = await self.workspace_connections.get(UUID(workspace_connection_id))
        else:
            resolved = await self._resolve_workspace(
                identity.channel_installation_id, identity.default_workspace_connection_id
            )
            ws = resolved if not isinstance(resolved, list) else (resolved[0] if resolved else None)
        if ws is None:
            raise RuntimeError("workspace must already be resolved before a flow is in progress")
        return FlowContext(
            prom_instance_id=identity.prom_instance_id,
            prom_user_id=identity.prom_user_id,
            workspace_slug=ws.prom_workspace_slug,
            correlation_id=correlation_id,
        )

    # --- Postback handling ------------------------------------------------------------------------

    async def _handle_postback(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        identity: LinkedIdentity,
        correlation_id: str,
    ) -> None:
        assert event.user_id is not None and event.postback_data is not None
        fields = decode_postback(event.postback_data)
        action = fields.get("action")

        if action == "cancel":
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._reply(
                channel_access_token, event.reply_token, msg.build_cancelled_message()
            )
            return

        if action == "select_workspace":
            await self._handle_workspace_selection(
                installation, channel_access_token, event, identity, fields
            )
            return

        if action == "select_work_item":
            new_state = self.update_flow.start_for_work_item(fields["project"], fields["value"])
            await self.conversation_states.set(
                installation.id, event.user_id, new_state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(
                channel_access_token, event.reply_token, msg.build_update_action_menu()
            )
            return

        if action in ("create", "my_tasks", "update"):
            # The menu's Quick Reply buttons (`build_menu_message`) encode these as postbacks via
            # `encode_postback("create"|"my_tasks"|"update")` — must route the same as the
            # identically-worded typed keywords ("สร้างงาน"/"งานของฉัน"/"อัปเดต") in
            # `_handle_text_message`, or tapping the button is a silent no-op (no conversation
            # state exists yet at this point, so the fallback below would just return).
            pending_action: _PendingAction = action  # type: ignore[assignment]
            await self._start_or_defer(
                installation, channel_access_token, event, identity, pending_action
            )
            return

        state = await self.conversation_states.get(installation.id, event.user_id)
        if state is None:
            return
        ctx = await self._flow_context(identity, state, correlation_id)

        if state.flow == "create":
            await self._handle_create_postback(
                installation, channel_access_token, event, ctx, state, action, fields
            )
        elif state.flow == "update":
            await self._handle_update_postback(
                installation, channel_access_token, event, ctx, state, action, fields
            )

    async def _handle_workspace_selection(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        identity: LinkedIdentity,
        fields: dict[str, str],
    ) -> None:
        assert event.user_id is not None
        state = await self.conversation_states.get(installation.id, event.user_id)
        if state is None or state.flow != "workspace_select":
            return
        ws = await self.workspace_connections.get(UUID(fields["value"]))
        if ws is None:
            return
        pending_action: _PendingAction = state.data["pending_action"]
        await self.conversation_states.clear(installation.id, event.user_id)
        await self._begin_action(
            installation, channel_access_token, event, identity, ws, pending_action
        )

    async def _handle_create_postback(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        ctx: FlowContext,
        state: ConversationState,
        action: str | None,
        fields: dict[str, str],
    ) -> None:
        assert event.user_id is not None

        if action == "select_project" and state.step == CreateFlowStep.PROJECT:
            next_state, states = await self.create_flow.submit_project(ctx, state, fields["value"])
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(
                channel_access_token, event.reply_token, msg.build_state_picker(states)
            )
            return

        if action == "select_state" and state.step == CreateFlowStep.STATE:
            state_id = None if fields["value"] == "skip" else fields["value"]
            next_state, members = await self.create_flow.submit_state(ctx, state, state_id)
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(
                channel_access_token, event.reply_token, msg.build_assignee_picker(members)
            )
            return

        if action == "select_assignee" and state.step == CreateFlowStep.ASSIGNEE:
            assignee_id = None if fields["value"] == "skip" else fields["value"]
            next_state = self.create_flow.submit_assignee(state, assignee_id)
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._reply(channel_access_token, event.reply_token, msg.build_due_date_prompt())
            return

        if action == "confirm" and state.step == CreateFlowStep.REVIEW:
            idempotency_key = (
                f"line-create:{installation.id}:{event.user_id}:"
                f"{state.data.get('title')}:{state.data.get('project_id')}"
            )
            work_item, was_replay = await self.create_flow.confirm(
                ctx, state, idempotency_key=idempotency_key
            )
            await self.conversation_states.clear(installation.id, event.user_id)

            projects = await self.list_projects.execute(
                prom_instance_id=ctx.prom_instance_id,
                prom_user_id=ctx.prom_user_id,
                workspace_slug=ctx.workspace_slug,
                correlation_id=ctx.correlation_id,
            )
            project = next((p for p in projects if p.id == work_item.project_id), None)
            instance = await self.prom_instances.get(ctx.prom_instance_id)
            web_url = (
                f"{instance.base_url}/{ctx.workspace_slug}/projects/"
                f"{work_item.project_id}/issues/{work_item.id}"
                if instance is not None
                else ""
            )
            await self._reply(
                channel_access_token,
                event.reply_token,
                msg.build_create_success_message(
                    work_item=work_item,
                    project_identifier=project.identifier if project else "",
                    web_url=web_url,
                    was_replay=was_replay,
                ),
            )

    async def _handle_update_postback(
        self,
        installation: ChannelInstallation,
        channel_access_token: str,
        event: LineEvent,
        ctx: FlowContext,
        state: ConversationState,
        action: str | None,
        fields: dict[str, str],
    ) -> None:
        assert event.user_id is not None

        if action == "select_update_action" and state.step == UpdateFlowStep.SELECT_ACTION:
            choice = fields["value"]
            if choice == "state":
                next_state, states = await self.update_flow.select_change_state(ctx, state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_state_picker(states)
                )
            elif choice == "assignee":
                next_state, members = await self.update_flow.select_reassign(ctx, state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_assignee_picker(members)
                )
            elif choice == "comment":
                next_state = self.update_flow.select_add_comment(state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_comment_prompt()
                )
            elif choice == "complete":
                next_state = self.update_flow.select_mark_complete(state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_confirm_complete_prompt()
                )
            return

        if action == "select_state" and state.step == UpdateFlowStep.AWAIT_STATE:
            state_id = fields["value"]
            if state_id == "skip":
                await self.conversation_states.clear(installation.id, event.user_id)
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_cancelled_message()
                )
                return
            await self.update_flow.confirm_state_change(ctx, state, state_id)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._reply(
                channel_access_token,
                event.reply_token,
                msg.build_update_success_message(action="state"),
            )
            return

        if action == "select_assignee" and state.step == UpdateFlowStep.AWAIT_ASSIGNEE:
            assignee_id = fields["value"]
            if assignee_id == "skip":
                await self.conversation_states.clear(installation.id, event.user_id)
                await self._reply(
                    channel_access_token, event.reply_token, msg.build_cancelled_message()
                )
                return
            await self.update_flow.confirm_reassign(ctx, state, assignee_id)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._reply(
                channel_access_token,
                event.reply_token,
                msg.build_update_success_message(action="assignee"),
            )
            return

        if action == "confirm" and state.step == UpdateFlowStep.CONFIRM_COMPLETE:
            await self.update_flow.confirm_mark_complete(ctx, state)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._reply(
                channel_access_token,
                event.reply_token,
                msg.build_update_success_message(action="complete"),
            )
