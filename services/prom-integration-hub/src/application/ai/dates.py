"""Rule-based resolution of Thai/English date phrases ("พรุ่งนี้", "วันศุกร์", "สิ้นเดือน",
"อีก 2 วัน", "next friday", "25/12/2569") to a `date`. Deliberately deterministic and tested
exhaustively (tests/ai/test_dates.py) rather than left to the LLM: a wrong due date is a silent,
costly error, and these phrases are a small closed set. The LLM's own ISO guess is only used when
these rules return None (see assistant.py).

Conventions (documented because each is a judgement call):
- A bare weekday ("วันศุกร์", "friday") means its next occurrence, *including today*.
- "<weekday>หน้า" / "next <weekday>" means that weekday in next calendar week (Mon-Sun weeks).
- "สิ้นสัปดาห์" / "end of week" means this week's Friday (end of the working week); if today is
  already past Friday, next week's Friday.
- Relative shifts ("อีก 2 วัน", "+2d", "in 2 days") are applied to `base`, which callers set to
  the work item's current due date when rescheduling, and to today otherwise.
- A Buddhist-era year (> 2400) is converted to CE by subtracting 543.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_WEEKDAYS: dict[str, int] = {
    "จันทร์": 0,
    "อังคาร": 1,
    "พุธ": 2,
    "พฤหัสบดี": 3,
    "พฤหัส": 3,
    "ศุกร์": 4,
    "เสาร์": 5,
    "อาทิตย์": 6,
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_THAI_NUMBERS: dict[str, int] = {
    "หนึ่ง": 1,
    "สอง": 2,
    "สาม": 3,
    "สี่": 4,
    "ห้า": 5,
    "หก": 6,
    "เจ็ด": 7,
    "แปด": 8,
    "เก้า": 9,
    "สิบ": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

_FILLER = re.compile(
    r"(?:เลื่อนไป|เลื่อน|ภายใน|ก่อน|ถึง|ส่ง|ของ|เป็น|ใน|ที่|นี้)|\b(?:due|by|on|the|this|to)\b",
    re.IGNORECASE,
)

_SHIFT = re.compile(
    r"^(?:อีก|เลื่อน(?:ไป)?(?:อีก)?|\+|in|plus|after)?\s*"
    r"(?P<n>\d+|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า|สิบ|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s*(?P<unit>วัน|days?|d|สัปดาห์|อาทิตย์|weeks?|w|เดือน|months?)$",
    re.IGNORECASE,
)

_NUMERIC_DATE = re.compile(r"^(?P<d>\d{1,2})[/\-.](?P<m>\d{1,2})(?:[/\-.](?P<y>\d{2,4}))?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize(text: str) -> str:
    text = text.translate(_THAI_DIGITS).strip().lower()
    text = text.replace("วัน", " วัน ").replace("หน้า", " หน้า ")
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _to_int(token: str) -> int:
    return int(token) if token.isdigit() else _THAI_NUMBERS[token.lower()]


def _next_weekday(today: date, weekday: int) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _weekday_next_week(today: date, weekday: int) -> date:
    monday_next_week = today + timedelta(days=7 - today.weekday())
    return monday_next_week + timedelta(days=weekday)


def _resolve_year(raw: str | None, today: date) -> int:
    if raw is None:
        return today.year
    year = int(raw)
    if year < 100:
        year += 2500 if year >= 50 else 2000  # "69" → 2569 BE, "26" → 2026 CE
    if year > 2400:
        year -= 543
    return year


def resolve_date_expression(
    expression: str | None, today: date, *, base: date | None = None
) -> date | None:
    """Returns None when the phrase isn't one of the supported forms — callers then fall back
    to the LLM's own ISO date (and show it for confirmation), never to a guess made here."""
    if not expression or not expression.strip():
        return None
    raw = expression.translate(_THAI_DIGITS).strip()
    if _ISO_DATE.match(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    text = _normalize(expression)
    compact = _compact(text)
    shift_base = base or today

    numeric = _NUMERIC_DATE.match(compact)
    if numeric:
        try:
            parsed = date(
                _resolve_year(numeric.group("y"), today),
                int(numeric.group("m")),
                int(numeric.group("d")),
            )
        except ValueError:
            return None
        # "25/12" with no year that's already passed this year means next year's.
        if numeric.group("y") is None and parsed < today:
            parsed = date(parsed.year + 1, parsed.month, parsed.day)
        return parsed

    shift = _SHIFT.match(compact) or _SHIFT.match(text)
    if shift:
        n = _to_int(shift.group("n"))
        unit = shift.group("unit").lower()
        if unit in ("วัน", "d", "day", "days"):
            return shift_base + timedelta(days=n)
        if unit in ("สัปดาห์", "อาทิตย์", "w", "week", "weeks"):
            return shift_base + timedelta(weeks=n)
        return _add_months(shift_base, n)

    if compact in ("วันนี้", "today"):
        return today
    if compact in ("พรุ่งนี้", "tomorrow", "tmr"):
        return today + timedelta(days=1)
    if compact in ("มะรืน", "มะรืนนี้", "dayaftertomorrow"):
        return today + timedelta(days=2)
    if compact in ("สิ้นเดือน", "สิ้นเดือนนี้", "endofmonth", "endofthemonth", "eom"):
        return date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if compact in ("สิ้นเดือนหน้า", "endofnextmonth"):
        nxt = _add_months(date(today.year, today.month, 1), 1)
        return date(nxt.year, nxt.month, calendar.monthrange(nxt.year, nxt.month)[1])
    if compact in ("สิ้นสัปดาห์", "สิ้นสัปดาห์นี้", "endofweek", "endofthisweek", "eow"):
        friday = _next_weekday(today, 4)
        return friday
    if compact in ("สัปดาห์หน้า", "อาทิตย์หน้า", "nextweek"):
        return _weekday_next_week(today, 0)

    without_filler = _compact(_FILLER.sub(" ", text))
    is_next_week = without_filler.endswith("หน้า") or without_filler.startswith("next")
    weekday_token = without_filler.removeprefix("next").removesuffix("หน้า")
    weekday_token = weekday_token.removeprefix("วัน").removesuffix("วัน")
    if weekday_token in _WEEKDAYS:
        weekday = _WEEKDAYS[weekday_token]
        return _weekday_next_week(today, weekday) if is_next_week else _next_weekday(today, weekday)

    return None
