"""The structured command the LLM is allowed to produce. The LLM only *describes* what the user
asked for, using the user's own words for every reference (project name, person, task name, date
phrase) — it never produces an id. Turning those references into Plane ids is done by
deterministic code (resolver.py), and every write still goes through a confirmation step
(assistant.py). See docs/architecture/04-adr-001-ai-intent-layer.md.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Intent(StrEnum):
    CREATE_WORK_ITEM = "create_work_item"
    LIST_MY_TASKS = "list_my_tasks"
    SEARCH = "search"
    CHANGE_STATE = "change_state"
    REASSIGN = "reassign"
    RESCHEDULE = "reschedule"
    ADD_COMMENT = "add_comment"
    DAILY_SUMMARY = "daily_summary"
    NOTIFICATION_SETTINGS = "notification_settings"
    HISTORY = "history"
    SHOW_MENU = "show_menu"
    UNKNOWN = "unknown"


StateTarget = Literal["started", "completed"]
TaskFilter = Literal["today", "overdue", "due_soon", "assigned"]
PriorityRef = Literal["urgent", "high", "medium", "low"]


class ParsedCommand(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: Intent
    title: str | None = None
    target_ref: str | None = None
    project_ref: str | None = None
    assignee_ref: str | None = None
    due_expression: str | None = None
    due_date: str | None = None
    state_ref: StateTarget | None = None
    filter: TaskFilter | None = None
    keyword: str | None = None
    comment: str | None = None
    priority: PriorityRef | None = None
    """Phase 2C: only when the user states it ("ด่วน", "urgent"); overrides auto-tagging."""


_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}

COMMAND_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "title",
        "target_ref",
        "project_ref",
        "assignee_ref",
        "due_expression",
        "due_date",
        "state_ref",
        "filter",
        "keyword",
        "comment",
        "priority",
    ],
    "properties": {
        "intent": {"type": "string", "enum": [i.value for i in Intent]},
        "title": _NULLABLE_STRING,
        "target_ref": _NULLABLE_STRING,
        "project_ref": _NULLABLE_STRING,
        "assignee_ref": _NULLABLE_STRING,
        "due_expression": _NULLABLE_STRING,
        "due_date": _NULLABLE_STRING,
        "state_ref": {"type": ["string", "null"], "enum": ["started", "completed", None]},
        "filter": {
            "type": ["string", "null"],
            "enum": ["today", "overdue", "due_soon", "assigned", None],
        },
        "keyword": _NULLABLE_STRING,
        "comment": _NULLABLE_STRING,
        "priority": {
            "type": ["string", "null"],
            "enum": ["urgent", "high", "medium", "low", None],
        },
    },
}
"""Hand-written rather than ParsedCommand.model_json_schema(): OpenAI-style strict mode needs
every property listed in `required` and `additionalProperties: false`, which pydantic's
generated schema does not produce for optional fields."""
