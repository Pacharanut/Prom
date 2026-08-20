"""Channel-agnostic conversation state machines for the Create-Work-Item and Update-Work-Item
flows (master prompt §8.2/§8.4). These orchestrate Gate 4's work-item use cases and own the
*sequence* of steps; a channel controller (src/channels/line/handler.py now, a Discord
counterpart in Gate 6) only needs to: persist/restore `ConversationState` between turns, turn raw
channel input (text/postback data) into the typed values these methods expect, and render
whatever list/summary each method returns as its own Flex/Embed/whatever. None of this module
knows LINE or Discord exist.

`ConversationState` is a plain, JSON-serializable dataclass so it can be stored in Redis between
webhook deliveries (Gate 5 adapter: src/adapters/redis/conversation_store.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    ListProjectMembers,
    ListProjects,
    ListStates,
    MarkComplete,
    UpdateWorkItemState,
)
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem, WorkItemState


class InvalidConversationInputError(ValueError):
    """A user's raw input (e.g. a due-date string) didn't parse — channel-agnostic, not a
    DomainError: this is input validation local to the conversation, not a Prom/Hub domain rule,
    so it deliberately does not go through the DomainError/error_code catalog. The channel
    controller catches this and re-prompts the same step, it never propagates further."""


class CreateFlowStep(StrEnum):
    TITLE = "title"
    PROJECT = "project"
    STATE = "state"
    ASSIGNEE = "assignee"
    DUE_DATE = "due_date"
    REVIEW = "review"


class UpdateFlowStep(StrEnum):
    SELECT_WORK_ITEM = "select_work_item"
    SELECT_ACTION = "select_action"
    AWAIT_STATE = "await_state"
    AWAIT_ASSIGNEE = "await_assignee"
    AWAIT_COMMENT = "await_comment"
    CONFIRM_COMPLETE = "confirm_complete"


_PROJECT_PICKER_CAP = 10
"""LINE Quick Reply/Flex button rows are a poor fit for long lists — capped, not paginated, in
Phase 1 (same "documented tradeoff, not an oversight" reasoning as Gate 4's my-tasks caps). A
workspace with more than 10 projects will not show all of them in the create-flow picker."""


@dataclass
class ConversationState:
    flow: Literal["create", "update", "workspace_select"]
    """"workspace_select" is not a real work-item flow — it's the channel-agnostic interstitial
    state used while a user with more than one linked workspace is asked to pick one for this
    session (src/channels/line/handler.py._start_or_defer). Modeled here rather than as
    channel-local state so Gate 6 doesn't have to reinvent it."""
    step: str
    data: dict[str, Any] = field(default_factory=dict)


def parse_due_date(
    text: str, *, skip_tokens: frozenset[str] = frozenset({"skip", "ข้าม", "-"})
) -> date | None:
    """Returns None for an explicit skip, a `date` for a valid `YYYY-MM-DD` string, or raises
    InvalidConversationInputError — never a date picker, per the master prompt's Phase 1 scope."""
    normalized = text.strip().lower()
    if normalized in skip_tokens:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise InvalidConversationInputError(
            f"{text!r} is not a valid YYYY-MM-DD date (or 'skip')"
        ) from exc


@dataclass
class FlowContext:
    """Everything every flow step needs about who's asking and where — threaded through instead
    of repeated as five separate parameters on every method."""

    prom_instance_id: UUID
    prom_user_id: str
    workspace_slug: str
    correlation_id: str


@dataclass
class CreateWorkItemFlow:
    list_projects: ListProjects
    list_states: ListStates
    list_project_members: ListProjectMembers
    create_work_item: CreateWorkItem

    def start(self) -> ConversationState:
        return ConversationState(flow="create", step=CreateFlowStep.TITLE)

    async def submit_title(
        self, ctx: FlowContext, state: ConversationState, title: str
    ) -> tuple[ConversationState, list[ProjectRef]]:
        title = title.strip()
        if not title:
            raise InvalidConversationInputError("title cannot be empty")
        projects = await self.list_projects.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            correlation_id=ctx.correlation_id,
        )
        next_state = ConversationState(
            flow="create", step=CreateFlowStep.PROJECT, data={**state.data, "title": title}
        )
        return next_state, projects[:_PROJECT_PICKER_CAP]

    async def submit_project(
        self, ctx: FlowContext, state: ConversationState, project_id: str
    ) -> tuple[ConversationState, list[WorkItemState]]:
        states = await self.list_states.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=project_id,
            correlation_id=ctx.correlation_id,
        )
        next_state = ConversationState(
            flow="create",
            step=CreateFlowStep.STATE,
            data={**state.data, "project_id": project_id},
        )
        return next_state, states

    async def submit_state(
        self, ctx: FlowContext, state: ConversationState, state_id: str | None
    ) -> tuple[ConversationState, list[ProjectMemberRef]]:
        """`state_id=None` means the user skipped — Plane assigns its own default state on
        create when none is given (the `state` field is optional/nullable on Issue), so skipping
        here is a legitimate choice, not just tolerated input."""
        members = await self.list_project_members.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            correlation_id=ctx.correlation_id,
        )
        next_state = ConversationState(
            flow="create",
            step=CreateFlowStep.ASSIGNEE,
            data={**state.data, "state_id": state_id},
        )
        return next_state, members

    def submit_assignee(
        self, state: ConversationState, assignee_id: str | None
    ) -> ConversationState:
        return ConversationState(
            flow="create",
            step=CreateFlowStep.DUE_DATE,
            data={**state.data, "assignee_id": assignee_id},
        )

    def submit_due_date_text(self, state: ConversationState, text: str) -> ConversationState:
        """Raises InvalidConversationInputError on unparseable input — the channel controller
        should catch it and re-prompt the same DUE_DATE step without advancing."""
        due_date = parse_due_date(text)
        return ConversationState(
            flow="create",
            step=CreateFlowStep.REVIEW,
            data={**state.data, "due_date": due_date.isoformat() if due_date else None},
        )

    async def confirm(
        self, ctx: FlowContext, state: ConversationState, *, idempotency_key: str
    ) -> tuple[WorkItem, bool]:
        due_date_str = state.data.get("due_date")
        return await self.create_work_item.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            title=state.data["title"],
            state_id=state.data.get("state_id"),
            assignee_ids=[state.data["assignee_id"]] if state.data.get("assignee_id") else None,
            due_date=date.fromisoformat(due_date_str) if due_date_str else None,
            idempotency_key=idempotency_key,
            correlation_id=ctx.correlation_id,
        )


@dataclass
class UpdateWorkItemFlow:
    list_states: ListStates
    list_project_members: ListProjectMembers
    update_work_item_state: UpdateWorkItemState
    change_assignee: ChangeAssignee
    add_comment: AddComment
    mark_complete: MarkComplete

    def start_for_work_item(self, project_id: str, work_item_id: str) -> ConversationState:
        """Entered either from the menu (after a work-item picker, not this class's concern) or
        directly from tapping a My-Tasks list item — either way, by the time this is called a
        specific work item has already been chosen."""
        return ConversationState(
            flow="update",
            step=UpdateFlowStep.SELECT_ACTION,
            data={"project_id": project_id, "work_item_id": work_item_id},
        )

    async def select_change_state(
        self, ctx: FlowContext, state: ConversationState
    ) -> tuple[ConversationState, list[WorkItemState]]:
        states = await self.list_states.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            correlation_id=ctx.correlation_id,
        )
        return (
            ConversationState(flow="update", step=UpdateFlowStep.AWAIT_STATE, data=state.data),
            states,
        )

    async def select_reassign(
        self, ctx: FlowContext, state: ConversationState
    ) -> tuple[ConversationState, list[ProjectMemberRef]]:
        members = await self.list_project_members.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            correlation_id=ctx.correlation_id,
        )
        return (
            ConversationState(flow="update", step=UpdateFlowStep.AWAIT_ASSIGNEE, data=state.data),
            members,
        )

    def select_add_comment(self, state: ConversationState) -> ConversationState:
        return ConversationState(flow="update", step=UpdateFlowStep.AWAIT_COMMENT, data=state.data)

    def select_mark_complete(self, state: ConversationState) -> ConversationState:
        """Mark Complete needs a confirmation step (like every other significant write here) even
        though it needs no further input — the master prompt requires confirmation before
        significant writes (§8.4), and MarkComplete unconditionally changes the work item's
        state, so it gets one like the others."""
        return ConversationState(
            flow="update", step=UpdateFlowStep.CONFIRM_COMPLETE, data=state.data
        )

    async def confirm_state_change(
        self, ctx: FlowContext, state: ConversationState, state_id: str
    ) -> WorkItem:
        return await self.update_work_item_state.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            work_item_id=state.data["work_item_id"],
            state_id=state_id,
            correlation_id=ctx.correlation_id,
        )

    async def confirm_reassign(
        self, ctx: FlowContext, state: ConversationState, assignee_id: str
    ) -> WorkItem:
        return await self.change_assignee.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            work_item_id=state.data["work_item_id"],
            assignee_ids=[assignee_id],
            correlation_id=ctx.correlation_id,
        )

    async def confirm_mark_complete(self, ctx: FlowContext, state: ConversationState) -> WorkItem:
        return await self.mark_complete.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            work_item_id=state.data["work_item_id"],
            correlation_id=ctx.correlation_id,
        )

    async def confirm_add_comment(
        self, ctx: FlowContext, state: ConversationState, comment_text: str
    ) -> str:
        """No separate confirmation step for comments (unlike state/assignee/complete): adding a
        comment doesn't change the work item's state or ownership, and the master prompt's
        confirmation requirement (§8.4) is scoped to writes "ที่มีผลสำคัญ" (significant-effect
        writes) — a comment is the lowest-risk action here, so it posts directly once the user
        submits the free-text body at AWAIT_COMMENT."""
        comment_html = f"<p>{comment_text}</p>"
        return await self.add_comment.execute(
            prom_instance_id=ctx.prom_instance_id,
            prom_user_id=ctx.prom_user_id,
            workspace_slug=ctx.workspace_slug,
            project_id=state.data["project_id"],
            work_item_id=state.data["work_item_id"],
            comment_html=comment_html,
            correlation_id=ctx.correlation_id,
        )
