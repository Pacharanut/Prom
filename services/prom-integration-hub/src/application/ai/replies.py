"""Structured, channel-agnostic replies the AiAssistant returns. Like every other application
module these carry data and codes, never user-facing copy — src/channels/ai_copy.py turns them
into Thai text, and the LINE/Discord presenters add buttons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from src.application.work_items.timeline import TimelineEntry

NoticeCode = Literal[
    "need_title",
    "no_projects",
    "project_not_found",
    "target_not_found",
    "need_target",
    "member_not_found",
    "member_ambiguous",
    "date_unreadable",
    "need_comment",
    "state_not_supported",
    "no_state_in_group",
    "need_search_criteria",
    "rate_limited",
    "session_expired",
    "error",
]


@dataclass
class Notice:
    code: NoticeCode
    detail: str | None = None
    """The user's own reference that failed to match ("Somchai"), candidate names, or — for
    code="error" — the DomainError.error_code."""


@dataclass
class NoticeReply:
    notice: Notice


@dataclass
class FallbackReply:
    """The channel should show its normal Phase 1 menu."""

    reason: Literal["disabled", "unavailable", "not_understood", "menu"]


@dataclass
class WorkItemRow:
    project_id: str
    work_item_id: str
    label: str
    """Display identifier, e.g. "PROM-12"."""
    name: str
    due_date: date | None = None


ListKind = Literal["assigned", "due_today", "overdue", "due_soon", "search", "project", "person"]


@dataclass
class ListReply:
    kind: ListKind
    rows: list[WorkItemRow]
    truncated: bool = False
    subject: str | None = None
    """What was searched for: the keyword, project name, or person's name."""


ConfirmAction = Literal["create", "start", "complete", "reassign", "reschedule", "comment"]


@dataclass
class ConfirmReply:
    action: ConfirmAction
    target_label: str | None = None
    title: str | None = None
    project_name: str | None = None
    state_name: str | None = None
    assignee_name: str | None = None
    due_date: date | None = None
    old_due_date: date | None = None
    comment: str | None = None
    warnings: list[Notice] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    """Phase 2C: label names that will be applied (create only)."""
    priority: str | None = None
    priority_is_explicit: bool = False
    tag_source: Literal["rule", "ai"] | None = None
    """Where the auto-tagging suggestion came from; None when nothing was suggested."""
    can_skip_suggestions: bool = False
    """True when there is an auto-suggestion the user may decline ("ยืนยัน (ไม่ใส่ tag)")."""


@dataclass
class ChoiceOption:
    value: str
    label: str


@dataclass
class ChoiceReply:
    kind: Literal["project", "work_item"]
    options: list[ChoiceOption]
    subject: str | None = None


@dataclass
class DoneReply:
    action: ConfirmAction
    target_label: str
    web_url: str | None = None
    was_replay: bool = False


@dataclass
class ShortcutReply:
    """Phase 2B: the user asked for something the channel already has a panel for — the
    handler opens it exactly as if the matching keyword had been typed."""

    kind: Literal["daily_summary", "notification_settings"]


@dataclass
class TimelineReply:
    """Phase 2E: a work item's change history, newest first."""

    target_label: str
    entries: list[TimelineEntry]


AssistantReply = (
    NoticeReply
    | FallbackReply
    | ListReply
    | ConfirmReply
    | ChoiceReply
    | DoneReply
    | ShortcutReply
    | TimelineReply
)
