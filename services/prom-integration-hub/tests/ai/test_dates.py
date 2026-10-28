"""resolve_date_expression — every supported Thai/English phrase, against a fixed Monday."""

from __future__ import annotations

from datetime import date

import pytest

from src.application.ai.dates import resolve_date_expression

_MONDAY = date(2026, 10, 26)
_FRIDAY = date(2026, 10, 30)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("วันนี้", date(2026, 10, 26)),
        ("today", date(2026, 10, 26)),
        ("พรุ่งนี้", date(2026, 10, 27)),
        ("tomorrow", date(2026, 10, 27)),
        ("มะรืน", date(2026, 10, 28)),
        ("วันศุกร์", date(2026, 10, 30)),
        ("ศุกร์", date(2026, 10, 30)),
        ("ส่งวันศุกร์", date(2026, 10, 30)),
        ("ภายในวันพฤหัส", date(2026, 10, 29)),
        ("เลื่อนไปวันพุธ", date(2026, 10, 28)),
        ("friday", date(2026, 10, 30)),
        ("by thursday", date(2026, 10, 29)),
        ("วันจันทร์", date(2026, 10, 26)),
        ("on monday", date(2026, 10, 26)),
        ("วันศุกร์หน้า", date(2026, 11, 6)),
        ("next friday", date(2026, 11, 6)),
        ("วันอาทิตย์", date(2026, 11, 1)),
        ("วันอาทิตย์หน้า", date(2026, 11, 8)),
        ("สัปดาห์หน้า", date(2026, 11, 2)),
        ("อาทิตย์หน้า", date(2026, 11, 2)),
        ("สิ้นเดือน", date(2026, 10, 31)),
        ("end of month", date(2026, 10, 31)),
        ("สิ้นเดือนหน้า", date(2026, 11, 30)),
        ("สิ้นสัปดาห์", date(2026, 10, 30)),
        ("อีก 2 วัน", date(2026, 10, 28)),
        ("อีกสองวัน", date(2026, 10, 28)),
        ("+3d", date(2026, 10, 29)),
        ("in 2 weeks", date(2026, 11, 9)),
        ("อีก 1 เดือน", date(2026, 11, 26)),
        ("2026-11-05", date(2026, 11, 5)),
        ("25/12", date(2026, 12, 25)),
        ("๒๕/๑๒", date(2026, 12, 25)),
        ("1/1/2570", date(2027, 1, 1)),
        ("1/1/2027", date(2027, 1, 1)),
    ],
)
def test_supported_phrases(expression: str, expected: date) -> None:
    assert resolve_date_expression(expression, _MONDAY) == expected


@pytest.mark.parametrize("expression", [None, "", "   ", "ไม่รู้", "เร็วๆ นี้", "31/02", "soon"])
def test_unsupported_phrases_return_none(expression: str | None) -> None:
    assert resolve_date_expression(expression, _MONDAY) is None


def test_relative_shift_applies_to_base_not_today() -> None:
    assert resolve_date_expression("อีก 2 วัน", _MONDAY, base=_FRIDAY) == date(2026, 11, 1)


def test_absolute_phrase_ignores_base() -> None:
    assert resolve_date_expression("พรุ่งนี้", _MONDAY, base=_FRIDAY) == date(2026, 10, 27)


def test_past_day_month_without_year_rolls_to_next_year() -> None:
    assert resolve_date_expression("1/1", _MONDAY) == date(2027, 1, 1)


def test_bare_weekday_on_that_weekday_means_today() -> None:
    assert resolve_date_expression("วันศุกร์", _FRIDAY) == _FRIDAY


def test_end_of_week_after_friday_means_next_friday() -> None:
    saturday = date(2026, 10, 31)
    assert resolve_date_expression("สิ้นสัปดาห์", saturday) == date(2026, 11, 6)
