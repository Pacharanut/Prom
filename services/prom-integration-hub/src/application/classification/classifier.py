"""Auto-tagging (Phase 2C, AI MVP item 8): suggest labels and a priority for a new work item.

Order: the workspace's keyword rules first (deterministic, configurable); only when no rule
matched, and the workspace allows it, the LLM picks from the project's *existing* labels — it
can never invent a label, and its answer is filtered against the real list. The result is only a
suggestion: callers show it in the confirmation step, and every failure here yields an empty
suggestion rather than blocking the create.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from src.application.ai.ports import AiConnection, LlmClient
from src.application.ai.settings import ResolveAiConnection
from src.application.classification.ports import ClassificationRepository
from src.application.work_items.use_cases import ListLabels
from src.domain.classification.entities import (
    PRIORITY_RANK,
    ClassificationRule,
    Priority,
)
from src.domain.work_items.entities import WorkItemLabel
from src.shared.errors import DomainError

_MAX_AI_LABELS = 3
_MAX_LABELS_IN_PROMPT = 60

_AI_PROMPT = """You tag a new task in a project-management tool. The task title may be Thai or English.
Choose 0-{max_labels} labels for it, ONLY from this list (copy names exactly): {labels}
Choose a priority only if the title clearly signals it (e.g. "ด่วน", "urgent", "ASAP", "critical", "production down" → urgent/high; "เล็กน้อย", "nice to have" → low); otherwise null.
Reply with the JSON object only."""


@dataclass
class ClassificationSuggestion:
    label_ids: list[str] = field(default_factory=list)
    label_names: list[str] = field(default_factory=list)
    priority: Priority | None = None
    source: Literal["rule", "ai"] | None = None

    @property
    def is_empty(self) -> bool:
        return not self.label_ids and self.priority is None


def apply_rules(
    title: str, rules: list[ClassificationRule], labels: list[WorkItemLabel]
) -> ClassificationSuggestion:
    """Every matching rule contributes: labels are unioned (only names that exist in this
    project), and the highest matching priority wins."""
    haystack = title.casefold()
    by_name = {label.name.casefold(): label for label in labels}
    chosen: dict[str, WorkItemLabel] = {}
    priority: Priority | None = None
    matched = False
    for rule in rules:
        if rule.keyword.casefold() not in haystack:
            continue
        if rule.label_name:
            label = by_name.get(rule.label_name.casefold())
            if label is not None:
                chosen[label.id] = label
                matched = True
        if rule.priority is not None:
            matched = True
            if priority is None or PRIORITY_RANK[rule.priority] > PRIORITY_RANK[priority]:
                priority = rule.priority
    if not matched:
        return ClassificationSuggestion()
    return ClassificationSuggestion(
        label_ids=list(chosen),
        label_names=[label.name for label in chosen.values()],
        priority=priority,
        source="rule",
    )


def _schema(label_names: list[str]) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "string"}
    if label_names:
        item["enum"] = label_names
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["labels", "priority"],
        "properties": {
            "labels": {"type": "array", "items": item, "maxItems": _MAX_AI_LABELS},
            "priority": {
                "type": ["string", "null"],
                "enum": [p.value for p in Priority] + [None],
            },
        },
    }


@dataclass
class SuggestClassification:
    classification: ClassificationRepository
    llm_client: LlmClient

    async def execute(
        self,
        *,
        workspace_connection_id: UUID,
        title: str,
        labels: list[WorkItemLabel],
        connection: AiConnection | None,
    ) -> ClassificationSuggestion:
        settings = await self.classification.get(workspace_connection_id)
        if not settings.is_enabled:
            return ClassificationSuggestion()
        from_rules = apply_rules(title, settings.rules, labels)
        if not from_rules.is_empty or not settings.use_ai_fallback or connection is None:
            return from_rules
        return await self._ask_ai(title, labels, connection)

    async def _ask_ai(
        self, title: str, labels: list[WorkItemLabel], connection: AiConnection
    ) -> ClassificationSuggestion:
        offered = labels[:_MAX_LABELS_IN_PROMPT]
        names = [label.name for label in offered]
        try:
            raw = await self.llm_client.complete_json(
                connection,
                system_prompt=_AI_PROMPT.format(
                    max_labels=_MAX_AI_LABELS, labels=", ".join(names) or "(no labels)"
                ),
                user_message=title[:300],
                schema_name="prom_classification",
                schema=_schema(names),
            )
        except DomainError:
            return ClassificationSuggestion()

        by_name = {label.name.casefold(): label for label in offered}
        chosen: dict[str, WorkItemLabel] = {}
        raw_labels = raw.get("labels")
        if isinstance(raw_labels, list):
            for name in raw_labels:
                label = by_name.get(str(name).casefold())
                if label is not None:
                    chosen[label.id] = label
        priority: Priority | None = None
        try:
            if raw.get("priority"):
                priority = Priority(str(raw["priority"]))
        except ValueError:
            priority = None
        if priority is Priority.NONE:
            priority = None
        if not chosen and priority is None:
            return ClassificationSuggestion()
        picked = list(chosen.values())[:_MAX_AI_LABELS]
        return ClassificationSuggestion(
            label_ids=[label.id for label in picked],
            label_names=[label.name for label in picked],
            priority=priority,
            source="ai",
        )


@dataclass
class AutoClassifier:
    """What the create flows call: fetches the project's labels with the user's own token,
    resolves the customer's AI connection, and asks SuggestClassification. Never raises."""

    list_labels: ListLabels
    suggest: SuggestClassification
    resolve_connection: ResolveAiConnection

    async def classify(
        self,
        *,
        customer_id: UUID,
        workspace_connection_id: UUID,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        title: str,
        correlation_id: str,
    ) -> ClassificationSuggestion:
        try:
            labels = await self.list_labels.execute(
                prom_instance_id=prom_instance_id,
                prom_user_id=prom_user_id,
                workspace_slug=workspace_slug,
                project_id=project_id,
                correlation_id=correlation_id,
            )
        except DomainError:
            labels = []
        try:
            connection = await self.resolve_connection.execute(customer_id)
            return await self.suggest.execute(
                workspace_connection_id=workspace_connection_id,
                title=title,
                labels=labels,
                connection=connection,
            )
        except DomainError:
            return ClassificationSuggestion()
