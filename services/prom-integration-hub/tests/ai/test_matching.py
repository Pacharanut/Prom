"""Deterministic reference matching — the only path from LLM words to Plane ids."""

from __future__ import annotations

from src.application.ai.matching import (
    is_self_ref,
    is_this_ref,
    match_members,
    match_projects,
    match_work_items,
    parse_identifier,
)
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemSearchResult

_PROJECTS = [
    ProjectRef(id="p-ai", name="AI Platform", identifier="AI"),
    ProjectRef(id="p-infra", name="Infrastructure", identifier="INFRA"),
    ProjectRef(id="p-web", name="Web App", identifier="WEB"),
]
_MEMBERS = [
    ProjectMemberRef(user_id="u-somchai", display_name="Somchai"),
    ProjectMemberRef(user_id="u-somsak", display_name="Somsak"),
    ProjectMemberRef(user_id="u-nok", display_name="Nok"),
    ProjectMemberRef(user_id="u-nok", display_name="Nok"),
]


def _result(id_: str, name: str, seq: int, ident: str = "AI") -> WorkItemSearchResult:
    return WorkItemSearchResult(
        id=id_, name=name, sequence_id=seq, project_id="p-ai", project_identifier=ident
    )


def test_project_exact_identifier_beats_substring() -> None:
    assert [p.id for p in match_projects("AI", _PROJECTS)] == ["p-ai"]


def test_project_word_prefix_is_stripped() -> None:
    assert [p.id for p in match_projects("project infra", _PROJECTS)] == ["p-infra"]
    assert [p.id for p in match_projects("โปรเจกต์ Web", _PROJECTS)] == ["p-web"]


def test_project_no_match() -> None:
    assert match_projects("Mobile", _PROJECTS) == []


def test_member_exact_match_and_dedupe() -> None:
    assert [m.user_id for m in match_members("nok", _MEMBERS)] == ["u-nok"]


def test_member_prefix_can_be_ambiguous() -> None:
    assert {m.user_id for m in match_members("Som", _MEMBERS)} == {"u-somchai", "u-somsak"}


def test_member_honorific_is_stripped() -> None:
    assert [m.user_id for m in match_members("คุณ Somchai", _MEMBERS)] == ["u-somchai"]


def test_self_and_this_refs() -> None:
    assert is_self_ref("ฉัน") and is_self_ref("me") and not is_self_ref("Nok")
    assert is_this_ref(None) and is_this_ref("นี้") and is_this_ref("this")
    assert not is_this_ref("API Gateway")


def test_parse_identifier() -> None:
    assert parse_identifier("prom-12") == ("PROM", 12)
    assert parse_identifier("API Gateway") is None


def test_work_item_identifier_match() -> None:
    results = [_result("a", "Login page", 3), _result("b", "API Gateway", 12)]
    assert [r.id for r in match_work_items("AI-12", results)] == ["b"]


def test_work_item_name_match_prefers_exact() -> None:
    results = [_result("a", "API Gateway v2", 3), _result("b", "API Gateway", 12)]
    assert [r.id for r in match_work_items("task API Gateway", results)] == ["b"]


def test_work_item_no_name_match_offers_all_search_results() -> None:
    results = [_result("a", "Something", 3), _result("b", "Else", 4)]
    assert [r.id for r in match_work_items("gateway", results)] == ["a", "b"]
