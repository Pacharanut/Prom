"""AiAssistant — the channel-agnostic AI command handler shared by LINE and Discord (Phase 2A:
spec items 1, 3, 6, 7). Flow for every message:

    text → IntentParser (LLM) → ParsedCommand (the user's own words, no ids)
         → deterministic resolution against live Plane data (matching.py, dates.py)
         → read-only intents: answered immediately
         → write intents: a ConfirmReply; nothing is written until confirm() is called
         → confirm(): the existing Phase 1 work-item use cases, with the user's own token

Anything the assistant can't do (AI off, gateway down, rate-limited, not understood) comes back
as a FallbackReply/NoticeReply — the channel then behaves exactly as it did in Phase 1.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src.application.account_linking.ports import (
    AuditLogRepository,
    Clock,
    PromInstanceRepository,
    RateLimiter,
)
from src.application.ai.commands import Intent, ParsedCommand
from src.application.ai.dates import resolve_date_expression
from src.application.ai.intent_parser import IntentParser
from src.application.ai.matching import (
    is_self_ref,
    is_this_ref,
    match_members,
    match_projects,
    match_work_items,
)
from src.application.ai.ports import FocusStore, WorkItemFocus
from src.application.ai.replies import (
    AssistantReply,
    ChoiceOption,
    ChoiceReply,
    ConfirmReply,
    DoneReply,
    FallbackReply,
    ListKind,
    ListReply,
    Notice,
    NoticeReply,
    ShortcutReply,
    TimelineReply,
    WorkItemRow,
)
from src.application.ai.settings import ResolveAiConnection
from src.application.classification.classifier import AutoClassifier, ClassificationSuggestion
from src.application.shared.audit_writer import AuditWriter
from src.application.work_items.conversation import ConversationState, FlowContext
from src.application.work_items.ports import ConversationStateRepository
from src.application.work_items.timeline import GetWorkItemTimeline
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    GetWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListProjectWorkItems,
    ListStates,
    MarkComplete,
    RescheduleWorkItem,
    SearchWorkItems,
    UpdateWorkItemState,
)
from src.domain.audit.entities import ResultStatus
from src.domain.integrations.entities import ChannelType
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem
from src.shared.errors import DomainError

_TIMEZONE = ZoneInfo("Asia/Bangkok")
_STATE_TTL_SECONDS = 600
_FOCUS_TTL_SECONDS = 30 * 60
_RATE_LIMIT = 30
_RATE_WINDOW_SECONDS = 600
_CHOICE_CAP = 10
_PERSON_SEARCH_PROJECT_CAP = 5
_SHIFT_HINT = re.compile(r"(อีก|เลื่อน|\+|\bin\b|\blater\b)", re.IGNORECASE)


@dataclass
class AssistantSession:
    """Who is asking and in which workspace — built by the channel controller once the user's
    LinkedIdentity and workspace are resolved."""

    customer_id: UUID
    channel_installation_id: UUID
    channel_type: ChannelType
    channel_user_id: str
    workspace_connection_id: UUID
    flow: FlowContext


@dataclass
class _Target:
    project_id: str
    work_item_id: str
    label: str


@dataclass
class AiAssistant:
    resolve_connection: ResolveAiConnection
    intent_parser: IntentParser
    conversation_states: ConversationStateRepository
    focus_store: FocusStore
    rate_limiter: RateLimiter
    prom_instances: PromInstanceRepository
    audit_log: AuditLogRepository
    clock: Clock
    list_projects: ListProjects
    list_states: ListStates
    list_project_members: ListProjectMembers
    list_my_work_items: ListMyWorkItems
    list_project_work_items: ListProjectWorkItems
    search_work_items: SearchWorkItems
    get_work_item: GetWorkItem
    create_work_item: CreateWorkItem
    update_work_item_state: UpdateWorkItemState
    mark_complete: MarkComplete
    change_assignee: ChangeAssignee
    reschedule_work_item: RescheduleWorkItem
    add_comment: AddComment
    auto_classifier: AutoClassifier | None = None
    """Phase 2C: suggests labels/priority for AI-created work items (shown before confirm)."""
    get_timeline: GetWorkItemTimeline | None = None
    """Phase 2E: "ประวัติงาน X" / "ใครแก้งานนี้"."""

    # --- Entry points ----------------------------------------------------------------------------

    async def is_enabled(self, customer_id: UUID) -> bool:
        return await self.resolve_connection.execute(customer_id) is not None

    async def handle_text(self, session: AssistantSession, text: str) -> AssistantReply:
        connection = await self.resolve_connection.execute(session.customer_id)
        if connection is None:
            return FallbackReply(reason="disabled")

        allowed = await self.rate_limiter.check_and_increment(
            key=f"ai:{session.channel_installation_id}:{session.channel_user_id}",
            limit=_RATE_LIMIT,
            window_seconds=_RATE_WINDOW_SECONDS,
        )
        if not allowed:
            return NoticeReply(Notice("rate_limited"))

        try:
            projects = await self._projects(session)
        except DomainError as exc:
            return NoticeReply(Notice("error", exc.error_code))

        try:
            command = await self.intent_parser.parse(
                connection, text, today=self._today(), project_names=[p.name for p in projects]
            )
        except DomainError as exc:
            await self._audit_parse(session, intent=None, error_code=exc.error_code)
            return FallbackReply(reason="unavailable")
        await self._audit_parse(session, intent=command.intent, error_code=None)

        try:
            return await self._dispatch(session, command, projects)
        except DomainError as exc:
            return NoticeReply(Notice("error", exc.error_code))

    async def handle_choice(
        self, session: AssistantSession, state: ConversationState, value: str
    ) -> AssistantReply:
        """The user tapped one of a ChoiceReply's options."""
        command = ParsedCommand.model_validate(state.data.get("command") or {"intent": "unknown"})
        try:
            if state.step == "pick_project":
                projects = await self._projects(session)
                project = next((p for p in projects if p.id == value), None)
                if project is None:
                    return NoticeReply(Notice("session_expired"))
                return await self._plan_create(session, command, project, warnings=[])
            if state.step == "pick_work_item":
                candidates: dict[str, str] = state.data.get("candidates") or {}
                if value not in candidates or ":" not in value:
                    return NoticeReply(Notice("session_expired"))
                project_id, work_item_id = value.split(":", 1)
                target = _Target(project_id, work_item_id, candidates[value])
                return await self._plan_write(session, command, target)
        except DomainError as exc:
            return NoticeReply(Notice("error", exc.error_code))
        return NoticeReply(Notice("session_expired"))

    async def confirm(
        self,
        session: AssistantSession,
        state: ConversationState,
        *,
        skip_suggestions: bool = False,
    ) -> AssistantReply:
        """Executes the write the user just confirmed. The pending action was fully resolved to
        ids before the ConfirmReply was shown — nothing is re-interpreted here.
        `skip_suggestions` ("ยืนยัน (ไม่ใส่ tag)") drops the auto-suggested labels/priority but
        keeps a priority the user stated themselves."""
        pending: dict[str, Any] | None = state.data.get("pending")
        await self.conversation_states.clear(
            session.channel_installation_id, session.channel_user_id
        )
        if state.step != "confirm" or not pending:
            return NoticeReply(Notice("session_expired"))
        if skip_suggestions:
            pending = {**pending, "label_ids": [], "auto_priority": None}
        try:
            return await self._execute(session, pending)
        except DomainError as exc:
            return NoticeReply(Notice("error", exc.error_code))

    async def remember_focus(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        project_id: str,
        work_item_id: str,
        label: str = "",
    ) -> None:
        """Called by channels when the user taps a work item in any list, so a following
        "mark complete" / "assign ให้ Nok" knows which one is meant."""
        await self.focus_store.set(
            channel_installation_id,
            channel_user_id,
            WorkItemFocus(project_id=project_id, work_item_id=work_item_id, label=label),
            _FOCUS_TTL_SECONDS,
        )

    # --- Dispatch ---------------------------------------------------------------------------------

    async def _dispatch(
        self, session: AssistantSession, command: ParsedCommand, projects: list[ProjectRef]
    ) -> AssistantReply:
        intent = command.intent
        if intent == Intent.CREATE_WORK_ITEM:
            return await self._create(session, command, projects)
        if intent == Intent.LIST_MY_TASKS:
            if command.filter is None and command.project_ref:
                return await self._search(session, command, projects)
            return await self._list_mine(session, command, projects)
        if intent == Intent.SEARCH:
            return await self._search(session, command, projects)
        if intent in (
            Intent.CHANGE_STATE,
            Intent.REASSIGN,
            Intent.RESCHEDULE,
            Intent.ADD_COMMENT,
            Intent.HISTORY,
        ):
            target = await self._resolve_target(session, command, projects)
            if not isinstance(target, _Target):
                return target
            return await self._plan_write(session, command, target)
        if intent == Intent.DAILY_SUMMARY:
            return ShortcutReply(kind="daily_summary")
        if intent == Intent.NOTIFICATION_SETTINGS:
            return ShortcutReply(kind="notification_settings")
        if intent == Intent.SHOW_MENU:
            return FallbackReply(reason="menu")
        return FallbackReply(reason="not_understood")

    # --- Read-only intents ------------------------------------------------------------------------

    async def _list_mine(
        self, session: AssistantSession, command: ParsedCommand, projects: list[ProjectRef]
    ) -> AssistantReply:
        view_by_filter: dict[str | None, ListKind] = {
            "today": "due_today",
            "overdue": "overdue",
            "due_soon": "due_soon",
            "assigned": "assigned",
            None: "assigned",
        }
        kind = view_by_filter[command.filter]
        scope = projects
        if command.project_ref:
            matched = match_projects(command.project_ref, projects)
            if not matched:
                return NoticeReply(Notice("project_not_found", command.project_ref))
            scope = matched
        result = await self.list_my_work_items.execute(
            prom_instance_id=session.flow.prom_instance_id,
            prom_user_id=session.flow.prom_user_id,
            workspace_slug=session.flow.workspace_slug,
            project_ids=[p.id for p in scope],
            view=kind,  # type: ignore[arg-type]
            correlation_id=session.flow.correlation_id,
        )
        rows = _rows(result.items, projects)
        await self._focus_if_single(session, rows)
        return ListReply(kind=kind, rows=rows, truncated=result.truncated)

    async def _search(
        self, session: AssistantSession, command: ParsedCommand, projects: list[ProjectRef]
    ) -> AssistantReply:
        matched: list[ProjectRef] = []
        if command.project_ref:
            matched = match_projects(command.project_ref, projects)
            if not matched:
                return NoticeReply(Notice("project_not_found", command.project_ref))

        if command.assignee_ref and is_self_ref(command.assignee_ref):
            mine = ParsedCommand(intent=Intent.LIST_MY_TASKS, filter="assigned")
            return await self._list_mine(session, mine, matched or projects)

        if command.assignee_ref:
            return await self._search_person(session, command.assignee_ref, matched or projects)

        if command.keyword:
            results = await self.search_work_items.execute(
                prom_instance_id=session.flow.prom_instance_id,
                prom_user_id=session.flow.prom_user_id,
                workspace_slug=session.flow.workspace_slug,
                query=command.keyword,
                limit=_CHOICE_CAP,
                workspace_search=len(matched) != 1,
                project_id=matched[0].id if len(matched) == 1 else None,
                correlation_id=session.flow.correlation_id,
            )
            rows = [
                WorkItemRow(
                    project_id=r.project_id,
                    work_item_id=r.id,
                    label=f"{r.project_identifier}-{r.sequence_id}",
                    name=r.name,
                )
                for r in results
            ]
            await self._focus_if_single(session, rows)
            return ListReply(kind="search", rows=rows, subject=command.keyword)

        if matched:
            project = matched[0]
            items = await self.list_project_work_items.execute(
                prom_instance_id=session.flow.prom_instance_id,
                prom_user_id=session.flow.prom_user_id,
                workspace_slug=session.flow.workspace_slug,
                project_id=project.id,
                correlation_id=session.flow.correlation_id,
            )
            rows = _rows(items, projects)
            await self._focus_if_single(session, rows)
            return ListReply(kind="project", rows=rows, subject=project.name)

        return NoticeReply(Notice("need_search_criteria"))

    async def _search_person(
        self, session: AssistantSession, person_ref: str, scope: list[ProjectRef]
    ) -> AssistantReply:
        rows: list[WorkItemRow] = []
        person_name: str | None = None
        for project in scope[:_PERSON_SEARCH_PROJECT_CAP]:
            members = await self._members(session, project.id)
            hits = match_members(person_ref, members)
            if len(hits) != 1:
                continue
            person_name = hits[0].display_name
            items = await self.list_project_work_items.execute(
                prom_instance_id=session.flow.prom_instance_id,
                prom_user_id=session.flow.prom_user_id,
                workspace_slug=session.flow.workspace_slug,
                project_id=project.id,
                assignee_id=hits[0].user_id,
                correlation_id=session.flow.correlation_id,
            )
            rows.extend(_rows(items, scope))
        if person_name is None:
            return NoticeReply(Notice("member_not_found", person_ref))
        return ListReply(
            kind="person",
            rows=rows[:_CHOICE_CAP],
            truncated=len(scope) > _PERSON_SEARCH_PROJECT_CAP,
            subject=person_name,
        )

    # --- Create -----------------------------------------------------------------------------------

    async def _create(
        self, session: AssistantSession, command: ParsedCommand, projects: list[ProjectRef]
    ) -> AssistantReply:
        if not (command.title and command.title.strip()):
            return NoticeReply(Notice("need_title"))
        if not projects:
            return NoticeReply(Notice("no_projects"))

        warnings: list[Notice] = []
        hits = match_projects(command.project_ref, projects) if command.project_ref else []
        if command.project_ref and not hits:
            warnings.append(Notice("project_not_found", command.project_ref))
        if len(hits) == 1:
            return await self._plan_create(session, command, hits[0], warnings=warnings)
        if not hits and len(projects) == 1:
            return await self._plan_create(session, command, projects[0], warnings=warnings)

        options = hits or projects
        await self._set_state(session, "pick_project", command=command)
        return ChoiceReply(
            kind="project",
            options=[ChoiceOption(value=p.id, label=p.name) for p in options[:_CHOICE_CAP]],
            subject=command.title,
        )

    async def _plan_create(
        self,
        session: AssistantSession,
        command: ParsedCommand,
        project: ProjectRef,
        *,
        warnings: list[Notice],
    ) -> AssistantReply:
        state_id: str | None = None
        state_name: str | None = None
        if command.state_ref:
            states = await self.list_states.execute(
                prom_instance_id=session.flow.prom_instance_id,
                prom_user_id=session.flow.prom_user_id,
                workspace_slug=session.flow.workspace_slug,
                project_id=project.id,
                correlation_id=session.flow.correlation_id,
            )
            chosen = next((s for s in states if s.group == command.state_ref), None)
            if chosen is not None:
                state_id, state_name = chosen.id, chosen.name

        assignee: ProjectMemberRef | None = None
        if command.assignee_ref:
            members = await self._members(session, project.id)
            assignee, warning = self._pick_member(session, command.assignee_ref, members)
            if warning is not None:
                warnings.append(warning)

        due = self._resolve_due(command, base=None)
        if command.due_expression and due is None:
            warnings.append(Notice("date_unreadable", command.due_expression))

        title = (command.title or "").strip()
        pending: dict[str, Any] = {
            "op": "create",
            "project_id": project.id,
            "project_identifier": project.identifier,
            "title": title,
            "state_id": state_id,
            "assignee_id": assignee.user_id if assignee else None,
            "due_date": due.isoformat() if due else None,
        }
        suggestion = ClassificationSuggestion()
        if self.auto_classifier is not None:
            suggestion = await self.auto_classifier.classify(
                customer_id=session.customer_id,
                workspace_connection_id=session.workspace_connection_id,
                prom_instance_id=session.flow.prom_instance_id,
                prom_user_id=session.flow.prom_user_id,
                workspace_slug=session.flow.workspace_slug,
                project_id=project.id,
                title=title,
                correlation_id=session.flow.correlation_id,
            )
        explicit_priority = command.priority
        pending["label_ids"] = suggestion.label_ids
        pending["auto_priority"] = suggestion.priority.value if suggestion.priority else None
        pending["priority"] = explicit_priority
        shown_priority = explicit_priority or pending["auto_priority"]
        await self._set_state(session, "confirm", command=command, pending=pending)
        return ConfirmReply(
            action="create",
            title=title,
            project_name=project.name,
            state_name=state_name,
            assignee_name=assignee.display_name if assignee else None,
            due_date=due,
            warnings=warnings,
            labels=suggestion.label_names,
            priority=shown_priority,
            priority_is_explicit=explicit_priority is not None,
            tag_source=suggestion.source,
            can_skip_suggestions=not suggestion.is_empty,
        )

    # --- Writes on an existing work item ----------------------------------------------------------

    async def _resolve_target(
        self, session: AssistantSession, command: ParsedCommand, projects: list[ProjectRef]
    ) -> _Target | AssistantReply:
        if is_this_ref(command.target_ref):
            focus = await self.focus_store.get(
                session.channel_installation_id, session.channel_user_id
            )
            if focus is None:
                return NoticeReply(Notice("need_target"))
            label = focus.label or await self._label_for(
                session, focus.project_id, focus.work_item_id, projects
            )
            return _Target(focus.project_id, focus.work_item_id, label)

        ref = command.target_ref or ""
        results = await self.search_work_items.execute(
            prom_instance_id=session.flow.prom_instance_id,
            prom_user_id=session.flow.prom_user_id,
            workspace_slug=session.flow.workspace_slug,
            query=ref,
            limit=_CHOICE_CAP,
            correlation_id=session.flow.correlation_id,
        )
        hits = match_work_items(ref, results)
        if not hits:
            return NoticeReply(Notice("target_not_found", ref))
        if len(hits) == 1:
            hit = hits[0]
            return _Target(
                hit.project_id, hit.id, f"{hit.project_identifier}-{hit.sequence_id} {hit.name}"
            )
        candidates = {
            f"{h.project_id}:{h.id}": f"{h.project_identifier}-{h.sequence_id} {h.name}"
            for h in hits[:_CHOICE_CAP]
        }
        await self._set_state(session, "pick_work_item", command=command, candidates=candidates)
        return ChoiceReply(
            kind="work_item",
            options=[ChoiceOption(value=v, label=label) for v, label in candidates.items()],
            subject=ref,
        )

    async def _plan_write(
        self, session: AssistantSession, command: ParsedCommand, target: _Target
    ) -> AssistantReply:
        await self.remember_focus(
            session.channel_installation_id,
            session.channel_user_id,
            target.project_id,
            target.work_item_id,
            target.label,
        )
        if command.intent == Intent.HISTORY:
            return await self._timeline(session, target)
        base_pending = {
            "project_id": target.project_id,
            "work_item_id": target.work_item_id,
            "label": target.label,
        }

        if command.intent == Intent.CHANGE_STATE:
            if command.state_ref == "completed":
                await self._set_state(
                    session, "confirm", command=command, pending={**base_pending, "op": "complete"}
                )
                return ConfirmReply(action="complete", target_label=target.label)
            if command.state_ref == "started":
                states = await self.list_states.execute(
                    prom_instance_id=session.flow.prom_instance_id,
                    prom_user_id=session.flow.prom_user_id,
                    workspace_slug=session.flow.workspace_slug,
                    project_id=target.project_id,
                    correlation_id=session.flow.correlation_id,
                )
                started = next((s for s in states if s.group == "started"), None)
                if started is None:
                    return NoticeReply(Notice("no_state_in_group", "started"))
                pending = {**base_pending, "op": "start", "state_id": started.id}
                await self._set_state(session, "confirm", command=command, pending=pending)
                return ConfirmReply(
                    action="start", target_label=target.label, state_name=started.name
                )
            return NoticeReply(Notice("state_not_supported"))

        if command.intent == Intent.REASSIGN:
            if not command.assignee_ref:
                return NoticeReply(Notice("member_not_found"))
            members = await self._members(session, target.project_id)
            assignee, warning = self._pick_member(session, command.assignee_ref, members)
            if assignee is None:
                return NoticeReply(warning or Notice("member_not_found", command.assignee_ref))
            pending = {**base_pending, "op": "reassign", "assignee_id": assignee.user_id}
            await self._set_state(session, "confirm", command=command, pending=pending)
            return ConfirmReply(
                action="reassign", target_label=target.label, assignee_name=assignee.display_name
            )

        if command.intent == Intent.RESCHEDULE:
            work_item = await self.get_work_item.execute(
                prom_instance_id=session.flow.prom_instance_id,
                prom_user_id=session.flow.prom_user_id,
                workspace_slug=session.flow.workspace_slug,
                project_id=target.project_id,
                work_item_id=target.work_item_id,
                correlation_id=session.flow.correlation_id,
            )
            new_due = self._resolve_due(command, base=work_item.target_date)
            if new_due is None:
                return NoticeReply(Notice("date_unreadable", command.due_expression))
            pending = {**base_pending, "op": "reschedule", "due_date": new_due.isoformat()}
            await self._set_state(session, "confirm", command=command, pending=pending)
            return ConfirmReply(
                action="reschedule",
                target_label=target.label,
                due_date=new_due,
                old_due_date=work_item.target_date,
            )

        if command.intent == Intent.ADD_COMMENT:
            if not (command.comment and command.comment.strip()):
                return NoticeReply(Notice("need_comment"))
            pending = {**base_pending, "op": "comment", "comment": command.comment.strip()}
            await self._set_state(session, "confirm", command=command, pending=pending)
            return ConfirmReply(
                action="comment", target_label=target.label, comment=command.comment.strip()
            )

        return FallbackReply(reason="not_understood")

    # --- Execute (after confirmation) -------------------------------------------------------------

    async def _execute(self, session: AssistantSession, pending: dict[str, Any]) -> AssistantReply:
        flow = session.flow
        op = pending["op"]
        if op == "create":
            due = pending.get("due_date")
            work_item, was_replay = await self.create_work_item.execute(
                origin=flow.origin,
                prom_instance_id=flow.prom_instance_id,
                prom_user_id=flow.prom_user_id,
                workspace_slug=flow.workspace_slug,
                project_id=pending["project_id"],
                title=pending["title"],
                state_id=pending.get("state_id"),
                assignee_ids=[pending["assignee_id"]] if pending.get("assignee_id") else None,
                due_date=date.fromisoformat(due) if due else None,
                idempotency_key=self._idempotency_key(session, pending),
                priority=pending.get("priority") or pending.get("auto_priority"),
                label_ids=pending.get("label_ids") or None,
                correlation_id=flow.correlation_id,
            )
            label = f"{pending.get('project_identifier') or ''}-{work_item.sequence_id}".lstrip("-")
            await self.remember_focus(
                session.channel_installation_id,
                session.channel_user_id,
                work_item.project_id,
                work_item.id,
                f"{label} {work_item.name}",
            )
            return DoneReply(
                action="create",
                target_label=f"{label} {work_item.name}",
                web_url=await self._web_url(session, work_item),
                was_replay=was_replay,
            )

        project_id: str = pending["project_id"]
        work_item_id: str = pending["work_item_id"]
        if op == "complete":
            work_item = await self.mark_complete.execute(
                origin=flow.origin,
                prom_instance_id=flow.prom_instance_id,
                prom_user_id=flow.prom_user_id,
                workspace_slug=flow.workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                correlation_id=flow.correlation_id,
            )
        elif op == "start":
            work_item = await self.update_work_item_state.execute(
                origin=flow.origin,
                prom_instance_id=flow.prom_instance_id,
                prom_user_id=flow.prom_user_id,
                workspace_slug=flow.workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                state_id=pending["state_id"],
                correlation_id=flow.correlation_id,
            )
        elif op == "reassign":
            work_item = await self.change_assignee.execute(
                origin=flow.origin,
                prom_instance_id=flow.prom_instance_id,
                prom_user_id=flow.prom_user_id,
                workspace_slug=flow.workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                assignee_ids=[pending["assignee_id"]],
                correlation_id=flow.correlation_id,
            )
        elif op == "reschedule":
            work_item = await self.reschedule_work_item.execute(
                origin=flow.origin,
                prom_instance_id=flow.prom_instance_id,
                prom_user_id=flow.prom_user_id,
                workspace_slug=flow.workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                due_date=date.fromisoformat(pending["due_date"]),
                correlation_id=flow.correlation_id,
            )
        elif op == "comment":
            await self.add_comment.execute(
                origin=flow.origin,
                prom_instance_id=flow.prom_instance_id,
                prom_user_id=flow.prom_user_id,
                workspace_slug=flow.workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                comment_html=_comment_html(pending["comment"]),
                correlation_id=flow.correlation_id,
            )
            return DoneReply(action="comment", target_label=pending["label"])
        else:
            return NoticeReply(Notice("session_expired"))
        return DoneReply(
            action=op,
            target_label=pending["label"],
            web_url=await self._web_url(session, work_item),
        )

    async def history_for_focus(self, session: AssistantSession) -> AssistantReply:
        """Phase 2E: the "ประวัติ" keyword — no LLM involved, works with AI switched off."""
        focus = await self.focus_store.get(session.channel_installation_id, session.channel_user_id)
        if focus is None:
            return NoticeReply(Notice("need_target"))
        try:
            label = focus.label or await self._label_for(
                session, focus.project_id, focus.work_item_id, await self._projects(session)
            )
            return await self._timeline(
                session, _Target(focus.project_id, focus.work_item_id, label)
            )
        except DomainError as exc:
            return NoticeReply(Notice("error", exc.error_code))

    async def _timeline(self, session: AssistantSession, target: _Target) -> AssistantReply:
        if self.get_timeline is None:
            return FallbackReply(reason="not_understood")
        entries = await self.get_timeline.execute(
            prom_instance_id=session.flow.prom_instance_id,
            prom_user_id=session.flow.prom_user_id,
            workspace_slug=session.flow.workspace_slug,
            project_id=target.project_id,
            work_item_id=target.work_item_id,
            limit=10,
            correlation_id=session.flow.correlation_id,
        )
        return TimelineReply(target_label=target.label, entries=entries)

    # --- Helpers ----------------------------------------------------------------------------------

    def _today(self) -> date:
        return self.clock.now().astimezone(_TIMEZONE).date()

    def _resolve_due(self, command: ParsedCommand, *, base: date | None) -> date | None:
        today = self._today()
        resolved = resolve_date_expression(command.due_expression, today, base=base)
        if resolved is not None:
            return resolved
        # Rules couldn't read the phrase: use the LLM's own ISO guess, which the user then sees
        # in the confirmation before anything is written. Never for a relative shift on an
        # existing due date — the LLM resolved that against today, not the item's due date.
        is_shift = bool(command.due_expression and _SHIFT_HINT.search(command.due_expression))
        if command.due_date and not (base is not None and is_shift):
            try:
                return date.fromisoformat(command.due_date)
            except ValueError:
                return None
        return None

    def _pick_member(
        self, session: AssistantSession, ref: str, members: list[ProjectMemberRef]
    ) -> tuple[ProjectMemberRef | None, Notice | None]:
        if is_self_ref(ref):
            me = next((m for m in members if m.user_id == session.flow.prom_user_id), None)
            return (me, None) if me else (None, Notice("member_not_found", ref))
        hits = match_members(ref, members)
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, Notice("member_not_found", ref)
        return None, Notice("member_ambiguous", ", ".join(m.display_name for m in hits[:5]))

    async def _projects(self, session: AssistantSession) -> list[ProjectRef]:
        return await self.list_projects.execute(
            prom_instance_id=session.flow.prom_instance_id,
            prom_user_id=session.flow.prom_user_id,
            workspace_slug=session.flow.workspace_slug,
            correlation_id=session.flow.correlation_id,
        )

    async def _members(self, session: AssistantSession, project_id: str) -> list[ProjectMemberRef]:
        return await self.list_project_members.execute(
            prom_instance_id=session.flow.prom_instance_id,
            prom_user_id=session.flow.prom_user_id,
            workspace_slug=session.flow.workspace_slug,
            project_id=project_id,
            correlation_id=session.flow.correlation_id,
        )

    async def _label_for(
        self,
        session: AssistantSession,
        project_id: str,
        work_item_id: str,
        projects: list[ProjectRef],
    ) -> str:
        work_item = await self.get_work_item.execute(
            prom_instance_id=session.flow.prom_instance_id,
            prom_user_id=session.flow.prom_user_id,
            workspace_slug=session.flow.workspace_slug,
            project_id=project_id,
            work_item_id=work_item_id,
            correlation_id=session.flow.correlation_id,
        )
        return f"{_rows([work_item], projects)[0].label} {work_item.name}"

    async def _focus_if_single(self, session: AssistantSession, rows: list[WorkItemRow]) -> None:
        if len(rows) == 1:
            row = rows[0]
            await self.remember_focus(
                session.channel_installation_id,
                session.channel_user_id,
                row.project_id,
                row.work_item_id,
                f"{row.label} {row.name}",
            )

    async def _set_state(
        self,
        session: AssistantSession,
        step: str,
        *,
        command: ParsedCommand,
        pending: dict[str, Any] | None = None,
        candidates: dict[str, str] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "workspace_connection_id": str(session.workspace_connection_id),
            "command": command.model_dump(mode="json"),
        }
        if pending is not None:
            data["pending"] = pending
        if candidates is not None:
            data["candidates"] = candidates
        await self.conversation_states.set(
            session.channel_installation_id,
            session.channel_user_id,
            ConversationState(flow="ai", step=step, data=data),
            _STATE_TTL_SECONDS,
        )

    def _idempotency_key(self, session: AssistantSession, pending: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(pending, sort_keys=True).encode()).hexdigest()[:24]
        return f"ai-create:{session.channel_installation_id}:{session.channel_user_id}:{digest}"

    async def _web_url(self, session: AssistantSession, work_item: WorkItem) -> str | None:
        instance = await self.prom_instances.get(session.flow.prom_instance_id)
        if instance is None:
            return None
        return (
            f"{instance.base_url}/{session.flow.workspace_slug}/projects/"
            f"{work_item.project_id}/issues/{work_item.id}"
        )

    async def _audit_parse(
        self, session: AssistantSession, *, intent: Intent | None, error_code: str | None
    ) -> None:
        """Records which intent was detected — never the message text (audit entries must not
        hold message content, see src/domain/audit/entities.py)."""
        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=session.customer_id,
            prom_instance_id=session.flow.prom_instance_id,
            installation_id=session.channel_installation_id,
            channel_type=session.channel_type,
            channel_user_id=session.channel_user_id,
            prom_user_id=session.flow.prom_user_id,
            action="ai.command_parsed" if error_code is None else "ai.command_parse_failed",
            target_type="ai_intent",
            target_id=intent.value if intent else None,
            correlation_id=session.flow.correlation_id,
            result_status=ResultStatus.SUCCESS if error_code is None else ResultStatus.FAILURE,
            error_code=error_code,
        )


def _rows(items: list[WorkItem], projects: list[ProjectRef]) -> list[WorkItemRow]:
    by_id = {p.id: p for p in projects}
    rows: list[WorkItemRow] = []
    for item in items:
        project = by_id.get(item.project_id)
        label = f"{project.identifier}-{item.sequence_id}" if project else str(item.sequence_id)
        rows.append(
            WorkItemRow(
                project_id=item.project_id,
                work_item_id=item.id,
                label=label,
                name=item.name,
                due_date=item.target_date,
            )
        )
    return rows


def _comment_html(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<p>{escaped}</p>"
