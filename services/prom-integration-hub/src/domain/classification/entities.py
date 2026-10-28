"""Auto-tagging settings (Phase 2C, AI MVP item 8) — per workspace connection, because Plane
labels and priorities are a workspace's own vocabulary. Rules are checked first; the LLM is only
asked when no rule matched and `use_ai_fallback` is on. Rules name labels by *name*: Plane
labels are per project, so the id is looked up in the target project at creation time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

MAX_RULES = 50
MAX_KEYWORD_LENGTH = 100


class Priority(StrEnum):
    """Plane's own priority values (plane/apps/api/plane/db/models/issue.py PRIORITY_CHOICES)."""

    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


PRIORITY_RANK: dict[Priority, int] = {
    Priority.URGENT: 4,
    Priority.HIGH: 3,
    Priority.MEDIUM: 2,
    Priority.LOW: 1,
    Priority.NONE: 0,
}


class InvalidClassificationRuleError(ValueError):
    """A rule with no keyword, an over-long keyword, or nothing to apply."""


@dataclass
class ClassificationRule:
    keyword: str
    """Matched case-insensitively as a substring of the work item title."""
    label_name: str | None
    priority: Priority | None

    def __post_init__(self) -> None:
        self.keyword = self.keyword.strip()
        self.label_name = (self.label_name or "").strip() or None
        if not self.keyword:
            raise InvalidClassificationRuleError("keyword must not be empty")
        if len(self.keyword) > MAX_KEYWORD_LENGTH:
            raise InvalidClassificationRuleError(
                f"keyword must be at most {MAX_KEYWORD_LENGTH} characters"
            )
        if self.label_name is None and self.priority is None:
            raise InvalidClassificationRuleError(
                f"rule {self.keyword!r} must set a label, a priority, or both"
            )


@dataclass
class ClassificationSettings:
    workspace_connection_id: UUID
    is_enabled: bool = True
    use_ai_fallback: bool = True
    rules: list[ClassificationRule] = field(default_factory=list)
    updated_at: datetime | None = None
    """None means "never saved" — the defaults above apply."""
