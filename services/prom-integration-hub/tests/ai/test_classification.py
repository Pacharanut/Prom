"""Phase 2C auto-tagging: rule matching, the AI fallback (and its guard rails), settings
validation, and the create flows applying — or skipping — the suggestion."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from src.application.ai.intent_parser import IntentParser
from src.application.classification.classifier import (
    AutoClassifier,
    SuggestClassification,
    apply_rules,
)
from src.application.classification.settings import RuleInput, UpdateClassificationSettings
from src.application.work_items.use_cases import ListLabels
from src.domain.classification.entities import (
    ClassificationRule,
    ClassificationSettings,
    InvalidClassificationRuleError,
    Priority,
)
from src.domain.work_items.entities import WorkItemLabel
from src.shared.errors import AiUnavailableError, PromRequestInvalidError
from tests.ai.conftest import AI_PROJECT, CHANNEL_USER, AiHarness
from tests.fakes.repositories import FakeClassificationRepository
from tests.fakes.support import FakeLlmClient

LABELS = [
    WorkItemLabel(id="l-bug", name="Bug"),
    WorkItemLabel(id="l-infra", name="Infra"),
    WorkItemLabel(id="l-ui", name="UI"),
]


def _rule(
    keyword: str, label: str | None = None, priority: Priority | None = None
) -> ClassificationRule:
    return ClassificationRule(keyword=keyword, label_name=label, priority=priority)


# --- apply_rules ------------------------------------------------------------------------------


def test_rules_union_labels_and_take_highest_priority() -> None:
    rules = [
        _rule("bug", "bug", Priority.MEDIUM),
        _rule("production", None, Priority.URGENT),
        _rule("server", "Infra"),
    ]
    s = apply_rules("Production server BUG on login", rules, LABELS)
    assert s.source == "rule"
    assert sorted(s.label_names) == ["Bug", "Infra"]
    assert s.priority is Priority.URGENT


def test_rule_for_label_missing_in_project_does_not_count() -> None:
    s = apply_rules("fix bug", [_rule("bug", "Defect")], LABELS)
    assert s.is_empty and s.source is None


def test_thai_keyword_matches() -> None:
    s = apply_rules("แก้บั๊กหน้า login ด่วน", [_rule("ด่วน", None, Priority.HIGH)], LABELS)
    assert s.priority is Priority.HIGH


def test_rule_needs_label_or_priority() -> None:
    with pytest.raises(InvalidClassificationRuleError):
        _rule("bug")
    with pytest.raises(InvalidClassificationRuleError):
        _rule("  ", "Bug")


# --- SuggestClassification ------------------------------------------------------------------


async def _suggest(
    *,
    title: str,
    settings: ClassificationSettings | None = None,
    llm: FakeLlmClient | None = None,
    connected: bool = True,
):  # type: ignore[no-untyped-def]
    from src.application.ai.ports import AiConnection

    repo = FakeClassificationRepository()
    ws = uuid4()
    if settings is not None:
        await repo.save(replace(settings, workspace_connection_id=ws))
    llm = llm or FakeLlmClient()
    result = await SuggestClassification(classification=repo, llm_client=llm).execute(
        workspace_connection_id=ws,
        title=title,
        labels=LABELS,
        connection=AiConnection("https://x/v1", "k", "m", 5) if connected else None,
    )
    return result, llm


@pytest.mark.asyncio
async def test_rule_hit_skips_llm() -> None:
    settings = ClassificationSettings(uuid4(), rules=[_rule("bug", "Bug")])
    result, llm = await _suggest(title="bug in form", settings=settings)
    assert result.label_names == ["Bug"] and result.source == "rule"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_ai_fallback_filters_invented_labels() -> None:
    llm = FakeLlmClient(default={"labels": ["UI", "Totally New"], "priority": "high"})
    result, _ = await _suggest(title="Redesign settings page", llm=llm)
    assert result.source == "ai"
    assert result.label_ids == ["l-ui"]
    assert result.priority is Priority.HIGH
    system_prompt = llm.calls[0][1]
    assert "Bug, Infra, UI" in system_prompt


@pytest.mark.asyncio
async def test_ai_none_priority_and_no_labels_is_empty() -> None:
    llm = FakeLlmClient(default={"labels": [], "priority": "none"})
    result, _ = await _suggest(title="x", llm=llm)
    assert result.is_empty


@pytest.mark.asyncio
async def test_ai_error_or_disabled_paths_are_empty() -> None:
    failing = FakeLlmClient(error=AiUnavailableError("down"))
    assert (await _suggest(title="x", llm=failing))[0].is_empty

    no_fallback = ClassificationSettings(uuid4(), use_ai_fallback=False)
    result, llm = await _suggest(title="x", settings=no_fallback)
    assert result.is_empty and llm.calls == []

    off = ClassificationSettings(uuid4(), is_enabled=False, rules=[_rule("x", "Bug")])
    assert (await _suggest(title="x", settings=off))[0].is_empty

    result, llm = await _suggest(title="x", connected=False)
    assert result.is_empty and llm.calls == []


# --- Settings use case ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_settings_validates(ai_harness: AiHarness) -> None:
    from src.domain.workspaces.entities import WorkspaceConnection
    from tests.ai.conftest import NOW
    from tests.fakes.repositories import FakeWorkspaceConnectionRepository

    workspaces = FakeWorkspaceConnectionRepository()
    ws = WorkspaceConnection(
        id=uuid4(),
        customer_id=ai_harness.customer_id,
        prom_instance_id=ai_harness.prom_instance_id,
        prom_workspace_id="w",
        prom_workspace_slug="acme",
        name="Acme",
        created_at=NOW,
        updated_at=NOW,
    )
    await workspaces.add(ws)
    repo = FakeClassificationRepository()
    update = UpdateClassificationSettings(
        workspace_connections=workspaces,
        classification=repo,
        audit_log=ai_harness.audit_log,
        clock=ai_harness.clock,
    )

    saved = await update.execute(
        workspace_connection_id=ws.id,
        is_enabled=True,
        use_ai_fallback=False,
        rules=[RuleInput(keyword=" bug ", label_name="Bug", priority="high")],
        correlation_id="c",
    )
    assert saved.rules[0].keyword == "bug" and saved.rules[0].priority is Priority.HIGH
    assert ai_harness.audit_log.entries[-1].action == "classification_settings.updated"

    for bad in (
        [RuleInput(keyword="", label_name="Bug", priority=None)],
        [RuleInput(keyword="x", label_name=None, priority=None)],
        [RuleInput(keyword="x", label_name=None, priority="critical")],
        [RuleInput(keyword=f"k{i}", label_name="Bug", priority=None) for i in range(51)],
    ):
        with pytest.raises(PromRequestInvalidError):
            await update.execute(
                workspace_connection_id=ws.id,
                is_enabled=True,
                use_ai_fallback=True,
                rules=bad,
                correlation_id="c",
            )


# --- Through the AI create flow ----------------------------------------------------------------


def _with_classifier(h: AiHarness, repo: FakeClassificationRepository):  # type: ignore[no-untyped-def]
    assistant = h.assistant
    assistant.auto_classifier = AutoClassifier(
        list_labels=h._uc(ListLabels),
        suggest=SuggestClassification(classification=repo, llm_client=h.llm_client),
        resolve_connection=h.resolve_connection,
    )
    return assistant


@pytest.mark.asyncio
async def test_ai_create_applies_rule_tags_after_confirm(ai_harness: AiHarness) -> None:
    h = ai_harness
    await h.enable_ai()
    h.prom_api_client.labels_by_project = {AI_PROJECT.id: list(LABELS)}
    repo = FakeClassificationRepository()
    assistant = _with_classifier(h, repo)
    session = h.session()
    await repo.save(
        ClassificationSettings(
            session.workspace_connection_id, rules=[_rule("bug", "Bug", Priority.HIGH)]
        )
    )
    h.says("t", intent="create_work_item", title="Fix login bug", project_ref="AI")

    reply = await assistant.handle_text(session, "t")
    assert reply.labels == ["Bug"]  # type: ignore[union-attr]
    assert reply.priority == "high" and reply.tag_source == "rule"  # type: ignore[union-attr]
    assert reply.can_skip_suggestions  # type: ignore[union-attr]

    state = await h.conversation_states.get(h.installation_id, CHANNEL_USER)
    assert state is not None
    await assistant.confirm(session, state)
    extras = next(iter(h.prom_api_client.created_extras.values()))
    assert extras == {"priority": "high", "label_ids": ["l-bug"]}


@pytest.mark.asyncio
async def test_skip_suggestions_keeps_explicit_priority(ai_harness: AiHarness) -> None:
    h = ai_harness
    await h.enable_ai()
    h.prom_api_client.labels_by_project = {AI_PROJECT.id: list(LABELS)}
    repo = FakeClassificationRepository()
    assistant = _with_classifier(h, repo)
    session = h.session()
    await repo.save(
        ClassificationSettings(session.workspace_connection_id, rules=[_rule("bug", "Bug")])
    )
    h.says("t", intent="create_work_item", title="Fix bug", project_ref="AI", priority="urgent")

    reply = await assistant.handle_text(session, "t")
    assert reply.priority == "urgent" and reply.priority_is_explicit  # type: ignore[union-attr]
    state = await h.conversation_states.get(h.installation_id, CHANNEL_USER)
    assert state is not None
    await assistant.confirm(session, state, skip_suggestions=True)
    extras = next(iter(h.prom_api_client.created_extras.values()))
    assert extras == {"priority": "urgent", "label_ids": []}


@pytest.mark.asyncio
async def test_create_without_classifier_is_unchanged(ai_harness: AiHarness) -> None:
    h = ai_harness
    await h.enable_ai()
    h.says("t", intent="create_work_item", title="Fix bug", project_ref="AI")
    reply = await h.assistant.handle_text(h.session(), "t")
    assert reply.labels == [] and reply.priority is None  # type: ignore[union-attr]
    assert not reply.can_skip_suggestions  # type: ignore[union-attr]


def test_intent_parser_schema_has_priority() -> None:
    from src.application.ai.commands import COMMAND_JSON_SCHEMA

    assert "priority" in COMMAND_JSON_SCHEMA["required"]
    assert IntentParser  # imported for symmetry with other tests
