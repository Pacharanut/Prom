"""DiscordWebhookHandler — the Discord counterpart to src/channels/line/handler.py's
LineWebhookHandler. Same responsibilities (account linking OTP, main menu, create-work-item flow,
my-tasks, update flow), same underlying channel-agnostic conversation flows
(src/application/work_items/conversation.py), same constructor-injected application-layer-only
dependencies. Two real differences from LINE, both transport-driven, not logic-driven:

1. `handle_event` takes one already-extracted `DiscordEvent` plus a `send` callback, rather than
   parsing a raw multi-event webhook payload — src/channels/discord/gateway_client.py extracts one
   DiscordEvent per discord.py Message/Interaction and calls this once each, since the Gateway
   delivers events individually rather than batched like LINE's HTTP webhook.
2. Replies are sent via the injected `send: SendFn` callback (bound by gateway_client.py to
   whatever discord.py object the event came from) rather than a module-level function using a
   reply_token — Discord has no equivalent of LINE's reply-token concept; `message.channel.send`/
   `interaction.response.send_message` are methods on the specific object that triggered the
   event, not a global "reply to X" API call, so the caller must close over the right target.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from src.channels.discord import components as comp
from src.channels.discord.components import DiscordMessage
from src.channels.discord.events import DiscordEvent
from src.domain.identities.entities import LinkedIdentity
from src.domain.integrations.entities import ChannelInstallation, ChannelType
from src.domain.workspaces.entities import WorkspaceConnection
from src.shared.errors import ChannelDeliveryFailedError, DomainError

_CONVERSATION_TTL_SECONDS = 600
_WEBHOOK_DEDUP_TTL_SECONDS = 24 * 60 * 60

_MENU_KEYWORDS = {"เมนู", "สวัสดี", "help", "menu", "hi", "hello"}
_CANCEL_KEYWORDS = {"ยกเลิก", "cancel"}

_PendingAction = Literal["create", "my_tasks", "update"]

SendFn = Callable[[DiscordMessage], Awaitable[None]]


def _is_otp_candidate(text: str) -> bool:
    return text.strip().isdigit() and len(text.strip()) == 6


@dataclass
class DiscordWebhookHandler:
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

    async def handle_event(
        self, installation: ChannelInstallation, event: DiscordEvent, send: SendFn
    ) -> None:
        if event.kind == "other":
            return
        is_new = await self.dedup_store.mark_seen_if_new(
            event.dedup_key(), ttl_seconds=_WEBHOOK_DEDUP_TTL_SECONDS
        )
        if not is_new:
            return

        correlation_id = str(uuid4())
        identity = await self.linked_identities.get_active_by_channel_user(
            installation.id, event.user_id
        )

        if event.kind == "message" and event.text is not None:
            await self._handle_message(
                installation, event, identity, event.text.strip(), correlation_id, send
            )
            return

        if event.kind == "component" and event.component_custom_id is not None:
            if identity is None:
                await self._send(send, comp.build_error_message("IDENTITY_NOT_LINKED"))
                return
            await self._handle_component(installation, event, identity, correlation_id, send)

    async def _send(self, send: SendFn, message: DiscordMessage) -> None:
        try:
            await send(message)
        except ChannelDeliveryFailedError:
            # Same reasoning as LineWebhookHandler._reply: a delivery failure here must not blow
            # up the caller's event loop / crash the Gateway event handler — nothing else to do.
            pass

    async def _handle_message(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        identity: LinkedIdentity | None,
        text: str,
        correlation_id: str,
        send: SendFn,
    ) -> None:
        state = await self.conversation_states.get(installation.id, event.user_id)
        if state is not None and text.lower() in {t.lower() for t in _CANCEL_KEYWORDS}:
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._send(send, comp.build_cancelled_message())
            return

        if identity is None:
            if _is_otp_candidate(text):
                await self._handle_otp(installation, event, text, correlation_id, send)
            else:
                await self._send(send, comp.build_error_message("IDENTITY_NOT_LINKED"))
            return

        if state is not None:
            await self._handle_flow_text_input(
                installation, event, identity, state, text, correlation_id, send
            )
            return

        lowered = text.lower()
        if lowered in {t.lower() for t in _MENU_KEYWORDS}:
            await self._send(send, comp.build_menu_message())
        elif text == "สร้างงาน":
            await self._start_or_defer(installation, event, identity, "create", send)
        elif text == "งานของฉัน":
            await self._start_or_defer(installation, event, identity, "my_tasks", send)
        elif text == "อัปเดต":
            await self._start_or_defer(installation, event, identity, "update", send)
        else:
            await self._send(send, comp.build_menu_message())

    # --- Account linking -------------------------------------------------------------------------

    async def _handle_otp(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        code: str,
        correlation_id: str,
        send: SendFn,
    ) -> None:
        try:
            identity = await self.consume_linking_code.execute(
                channel_installation_id=installation.id,
                channel_type=ChannelType.DISCORD,
                channel_user_id=event.user_id,
                submitted_code=code,
                correlation_id=correlation_id,
            )
        except DomainError as exc:
            await self._send(send, comp.build_error_message(exc.error_code))
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
            await self._send(
                send,
                comp.build_linking_success_message(
                    display_profile, has_multiple_workspaces=len(workspaces) > 1
                ),
            )
        else:
            await self._send(send, comp.build_cancelled_message())

    # --- Workspace resolution --------------------------------------------------------------------

    async def _resolve_workspace(
        self, installation_id: UUID, default_workspace_connection_id: UUID | None
    ) -> WorkspaceConnection | list[WorkspaceConnection]:
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
        event: DiscordEvent,
        identity: LinkedIdentity,
        action: _PendingAction,
        send: SendFn,
    ) -> None:
        resolved = await self._resolve_workspace(
            installation.id, identity.default_workspace_connection_id
        )

        if isinstance(resolved, list):
            if not resolved:
                await self._send(
                    send,
                    DiscordMessage(content="บัญชีนี้ยังไม่ได้เชื่อมกับ Workspace ใดเลย ติดต่อผู้ดูแลระบบ"),
                )
                return
            state = ConversationState(
                flow="workspace_select", step="select", data={"pending_action": action}
            )
            await self.conversation_states.set(
                installation.id, event.user_id, state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, comp.build_workspace_picker(resolved))
            return

        await self._begin_action(installation, event, identity, resolved, action, send)

    async def _begin_action(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        identity: LinkedIdentity,
        workspace: WorkspaceConnection,
        action: _PendingAction,
        send: SendFn,
    ) -> None:
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
            await self._send(send, comp.build_title_prompt())
        else:
            await self._show_my_tasks(installation, ctx, send)

    async def _show_my_tasks(
        self, installation: ChannelInstallation, ctx: FlowContext, send: SendFn
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
        await self._send(
            send, comp.build_my_tasks_list(result.items, projects_by_id, truncated=result.truncated)
        )

    # --- Free-text input mid-flow -----------------------------------------------------------------

    async def _handle_flow_text_input(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        identity: LinkedIdentity,
        state: ConversationState,
        text: str,
        correlation_id: str,
        send: SendFn,
    ) -> None:
        ctx = await self._flow_context(identity, state, correlation_id)

        if state.flow == "create" and state.step == CreateFlowStep.TITLE:
            try:
                next_state, projects = await self.create_flow.submit_title(ctx, state, text)
            except InvalidConversationInputError:
                await self._send(send, comp.build_title_prompt())
                return
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, comp.build_project_picker(projects))
            return

        if state.flow == "create" and state.step == CreateFlowStep.DUE_DATE:
            try:
                next_state = self.create_flow.submit_due_date_text(state, text)
            except InvalidConversationInputError:
                await self._send(send, comp.build_due_date_prompt())
                return
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, await self._review_card(ctx, next_state))
            return

        if state.flow == "update" and state.step == UpdateFlowStep.AWAIT_COMMENT:
            await self.update_flow.confirm_add_comment(ctx, state, text)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._send(send, comp.build_update_success_message(action="comment"))
            return

        await self._send(send, DiscordMessage(content="กรุณาเลือกจากตัวเลือกด้านบน หรือพิมพ์ 'ยกเลิก'"))

    async def _review_card(self, ctx: FlowContext, state: ConversationState) -> DiscordMessage:
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

        return comp.build_review_card(
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

    # --- Component (button/select) handling -------------------------------------------------------

    async def _handle_component(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        identity: LinkedIdentity,
        correlation_id: str,
        send: SendFn,
    ) -> None:
        custom_id = event.component_custom_id

        if custom_id == "cancel":
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._send(send, comp.build_cancelled_message())
            return

        _menu_actions: dict[str, _PendingAction] = {
            "menu_create": "create",
            "menu_my_tasks": "my_tasks",
            "menu_update": "update",
        }
        if custom_id in _menu_actions:
            await self._start_or_defer(
                installation, event, identity, _menu_actions[custom_id], send
            )
            return

        if custom_id == "select_workspace":
            await self._handle_workspace_selection(installation, event, identity, send)
            return

        if custom_id == "select_work_item":
            assert event.component_value is not None
            project_id, work_item_id = event.component_value.split(":", 1)
            new_state = self.update_flow.start_for_work_item(project_id, work_item_id)
            await self.conversation_states.set(
                installation.id, event.user_id, new_state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, comp.build_update_action_menu())
            return

        state = await self.conversation_states.get(installation.id, event.user_id)
        if state is None:
            await self._send(send, DiscordMessage(content="เซสชันหมดอายุแล้ว พิมพ์ 'เมนู' เพื่อเริ่มใหม่"))
            return
        ctx = await self._flow_context(identity, state, correlation_id)

        if state.flow == "create":
            await self._handle_create_component(installation, event, ctx, state, custom_id, send)
        elif state.flow == "update":
            await self._handle_update_component(installation, event, ctx, state, custom_id, send)

    async def _handle_workspace_selection(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        identity: LinkedIdentity,
        send: SendFn,
    ) -> None:
        state = await self.conversation_states.get(installation.id, event.user_id)
        if state is None or state.flow != "workspace_select":
            return
        assert event.component_value is not None
        ws = await self.workspace_connections.get(UUID(event.component_value))
        if ws is None:
            return
        pending_action: _PendingAction = state.data["pending_action"]
        await self.conversation_states.clear(installation.id, event.user_id)
        await self._begin_action(installation, event, identity, ws, pending_action, send)

    async def _handle_create_component(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        ctx: FlowContext,
        state: ConversationState,
        custom_id: str | None,
        send: SendFn,
    ) -> None:
        if custom_id == "select_project" and state.step == CreateFlowStep.PROJECT:
            assert event.component_value is not None
            next_state, states = await self.create_flow.submit_project(
                ctx, state, event.component_value
            )
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, comp.build_state_picker(states))
            return

        if custom_id == "select_state" and state.step == CreateFlowStep.STATE:
            assert event.component_value is not None
            state_id = None if event.component_value == "skip" else event.component_value
            next_state, members = await self.create_flow.submit_state(ctx, state, state_id)
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, comp.build_assignee_picker(members))
            return

        if custom_id == "select_assignee" and state.step == CreateFlowStep.ASSIGNEE:
            assert event.component_value is not None
            assignee_id = None if event.component_value == "skip" else event.component_value
            next_state = self.create_flow.submit_assignee(state, assignee_id)
            await self.conversation_states.set(
                installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
            )
            await self._send(send, comp.build_due_date_prompt())
            return

        if custom_id == "confirm" and state.step == CreateFlowStep.REVIEW:
            idempotency_key = (
                f"discord-create:{installation.id}:{event.user_id}:"
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
            await self._send(
                send,
                comp.build_create_success_message(
                    work_item=work_item,
                    project_identifier=project.identifier if project else "",
                    web_url=web_url,
                    was_replay=was_replay,
                ),
            )

    async def _handle_update_component(
        self,
        installation: ChannelInstallation,
        event: DiscordEvent,
        ctx: FlowContext,
        state: ConversationState,
        custom_id: str | None,
        send: SendFn,
    ) -> None:
        if custom_id == "select_update_action" and state.step == UpdateFlowStep.SELECT_ACTION:
            assert event.component_value is not None
            choice = event.component_value
            if choice == "state":
                next_state, states = await self.update_flow.select_change_state(ctx, state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._send(send, comp.build_state_picker(states))
            elif choice == "assignee":
                next_state, members = await self.update_flow.select_reassign(ctx, state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._send(send, comp.build_assignee_picker(members))
            elif choice == "comment":
                next_state = self.update_flow.select_add_comment(state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._send(send, comp.build_comment_prompt())
            elif choice == "complete":
                next_state = self.update_flow.select_mark_complete(state)
                await self.conversation_states.set(
                    installation.id, event.user_id, next_state, _CONVERSATION_TTL_SECONDS
                )
                await self._send(send, comp.build_confirm_complete_prompt())
            return

        if custom_id == "select_state" and state.step == UpdateFlowStep.AWAIT_STATE:
            assert event.component_value is not None
            if event.component_value == "skip":
                await self.conversation_states.clear(installation.id, event.user_id)
                await self._send(send, comp.build_cancelled_message())
                return
            await self.update_flow.confirm_state_change(ctx, state, event.component_value)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._send(send, comp.build_update_success_message(action="state"))
            return

        if custom_id == "select_assignee" and state.step == UpdateFlowStep.AWAIT_ASSIGNEE:
            assert event.component_value is not None
            if event.component_value == "skip":
                await self.conversation_states.clear(installation.id, event.user_id)
                await self._send(send, comp.build_cancelled_message())
                return
            await self.update_flow.confirm_reassign(ctx, state, event.component_value)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._send(send, comp.build_update_success_message(action="assignee"))
            return

        if custom_id == "confirm" and state.step == UpdateFlowStep.CONFIRM_COMPLETE:
            await self.update_flow.confirm_mark_complete(ctx, state)
            await self.conversation_states.clear(installation.id, event.user_id)
            await self._send(send, comp.build_update_success_message(action="complete"))
