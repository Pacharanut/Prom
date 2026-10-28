"""Work item DTOs. Unlike every other domain package in this codebase, these are NOT backed by a
Hub-owned table — per the master prompt's data-ownership rules (work items live only in
Prom/Plane, Hub must never persist them), these are plain read/write-through representations of
data fetched live from Prom's REST API on every call. No repository port, no SQLAlchemy model
exists for this package.

Field names and shapes were verified against the pinned `plane` upstream source (Gate 0/4
discovery), not guessed — see src/application/prom_api/ports.py for the exact endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class ProjectRef:
    id: str
    name: str
    identifier: str


@dataclass
class WorkItemState:
    id: str
    name: str
    group: str
    """One of backlog|unstarted|started|completed|cancelled. "Mark complete" means finding the
    state with group == "completed" for the project and PATCHing the work item to it — Plane has
    no separate boolean "is complete" field."""


@dataclass
class WorkItemLabel:
    """Phase 2C. Plane labels belong to one project — the same name can exist (with different
    ids) in several projects, which is why auto-tagging rules store names, not ids."""

    id: str
    name: str


@dataclass
class WorkItemActivity:
    """Phase 2E: one entry of Plane's own work-item history (IssueActivity)."""

    id: str
    verb: str
    """"created" / "updated" / "deleted"."""
    field: str | None
    """e.g. "state", "assignees", "target_date", "priority", "labels", "name", "comment"."""
    old_value: str | None
    new_value: str | None
    actor_id: str | None
    created_at: datetime


@dataclass
class ProjectMemberRef:
    user_id: str
    display_name: str


@dataclass
class WorkItem:
    id: str
    sequence_id: int
    project_id: str
    name: str
    state_id: str | None
    priority: str | None
    assignee_ids: list[str]
    target_date: date | None
    """Plane's field name for "due date" is `target_date`, not `due_date`."""
    completed_at: datetime | None


@dataclass
class WorkItemSearchResult:
    id: str
    name: str
    sequence_id: int
    project_id: str
    project_identifier: str
