"""Deterministic matching of the LLM's free-text references ("Somchai", "project AI", "API
Gateway", "PROM-12") against real Plane data. Pure functions — every id that reaches a write use
case comes from here (or from a button the user tapped), never from the LLM directly.

Each matcher returns *candidates*, best tier first, and stops at the first tier with any hit:
exact (case-insensitive) → prefix → substring. The caller decides what 0 / 1 / many means.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TypeVar

from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemSearchResult

T = TypeVar("T")

_SELF_REFS = frozenset({"me", "i", "myself", "ฉัน", "ผม", "เรา", "ตัวเอง", "หนู", "ดิฉัน", "กู"})
_THIS_REFS = frozenset(
    {"this", "it", "this task", "นี้", "อันนี้", "งานนี้", "task นี้", "ตัวนี้", "มัน", "อันนั้น"}
)
_IDENTIFIER = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{0,11})-(\d+)\s*$")
_PROJECT_PREFIX = re.compile(r"^(project|โปรเจกต์|โปรเจค|โปรเจ็ค|โปรเจ็กต์)\s*", re.IGNORECASE)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def is_self_ref(ref: str | None) -> bool:
    return ref is not None and normalize(ref) in _SELF_REFS


def is_this_ref(ref: str | None) -> bool:
    return ref is None or not ref.strip() or normalize(ref) in _THIS_REFS


def parse_identifier(ref: str) -> tuple[str, int] | None:
    """ "PROM-12" → ("PROM", 12)."""
    match = _IDENTIFIER.match(ref)
    if match is None:
        return None
    return match.group(1).upper(), int(match.group(2))


def _tiered(ref: str, items: Iterable[T], keys: Callable[[T], list[str]]) -> list[T]:
    needle = normalize(ref)
    if not needle:
        return []
    pool = list(items)
    for test in (
        lambda hay: hay == needle,
        lambda hay: hay.startswith(needle),
        lambda hay: needle in hay,
    ):
        hits = [item for item in pool if any(test(normalize(k)) for k in keys(item))]
        if hits:
            return hits
    return []


def match_projects(ref: str, projects: list[ProjectRef]) -> list[ProjectRef]:
    cleaned = _PROJECT_PREFIX.sub("", ref.strip())
    return _tiered(cleaned, projects, lambda p: [p.name, p.identifier])


def match_members(ref: str, members: list[ProjectMemberRef]) -> list[ProjectMemberRef]:
    cleaned = re.sub(r"^(คุณ|khun|k\.|น้อง|พี่)\s*", "", ref.strip(), flags=re.IGNORECASE)
    hits = _tiered(cleaned, members, lambda m: [m.display_name])
    # Several project-member rows can describe the same user — dedupe by user_id.
    seen: set[str] = set()
    unique: list[ProjectMemberRef] = []
    for member in hits:
        if member.user_id not in seen:
            seen.add(member.user_id)
            unique.append(member)
    return unique


def match_work_items(ref: str, results: list[WorkItemSearchResult]) -> list[WorkItemSearchResult]:
    identifier = parse_identifier(ref)
    if identifier is not None:
        prefix, sequence_id = identifier
        exact = [
            r
            for r in results
            if r.project_identifier.upper() == prefix and r.sequence_id == sequence_id
        ]
        if exact:
            return exact
    cleaned = re.sub(r"^(task|งาน)\s*", "", ref.strip(), flags=re.IGNORECASE)
    # No name tier matched: Plane's own search still found these (it also matches on fields we
    # don't see), so offer all of them as candidates rather than claiming nothing was found.
    return _tiered(cleaned, results, lambda r: [r.name]) or list(results)
