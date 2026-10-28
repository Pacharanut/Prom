"""Thai copy for the work-item timeline (Phase 2E), shared by LINE and Discord."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from src.application.work_items.timeline import TimelineEntry
from src.channels.ai_copy import PRIORITY_LABELS

_TZ = ZoneInfo("Asia/Bangkok")
_MONTHS = (
    "ม.ค.",
    "ก.พ.",
    "มี.ค.",
    "เม.ย.",
    "พ.ค.",
    "มิ.ย.",
    "ก.ค.",
    "ส.ค.",
    "ก.ย.",
    "ต.ค.",
    "พ.ย.",
    "ธ.ค.",
)
_SOURCES = {"line": "💬 LINE", "discord": "🎮 Discord", "hub": "🔗 Hub", "plane": "🌐 Plane"}
_KINDS = {
    "created": "สร้างงาน",
    "comment": "เพิ่ม Comment",
    "state": "สถานะ",
    "assignees": "ผู้รับผิดชอบ",
    "target_date": "กำหนดส่ง",
    "start_date": "วันเริ่ม",
    "priority": "Priority",
    "labels": "Label",
    "name": "ชื่องาน",
    "description": "รายละเอียด",
    "parent": "งานแม่",
    "estimate_point": "Estimate",
}
_MAX_VALUE = 40


def _short(value: str | None) -> str:
    if not value:
        return "—"
    value = value.strip()
    return value if len(value) <= _MAX_VALUE else value[: _MAX_VALUE - 1] + "…"


def _value(kind: str, value: str | None) -> str:
    if kind == "priority" and value:
        return PRIORITY_LABELS.get(value, value)
    return _short(value)


def entry_line(entry: TimelineEntry) -> str:
    local = entry.at.astimezone(_TZ)
    when = f"{local.day} {_MONTHS[local.month - 1]} {local:%H:%M}"
    who = entry.actor_name or "ไม่ทราบชื่อ"
    what = _KINDS.get(entry.kind, entry.kind)
    if entry.kind in ("created", "comment"):
        detail = what
    elif entry.old_value and entry.new_value:
        detail = (
            f"{what}: {_value(entry.kind, entry.old_value)} → {_value(entry.kind, entry.new_value)}"
        )
    elif entry.new_value:
        detail = f"{what}: + {_value(entry.kind, entry.new_value)}"
    elif entry.old_value:
        detail = f"{what}: − {_value(entry.kind, entry.old_value)}"
    else:
        detail = what
    return f"{when} · {_SOURCES[entry.source]} · {who} — {detail}"


def timeline_text(target_label: str | None, entries: list[TimelineEntry]) -> str:
    head = f"🕘 ประวัติงาน {target_label}" if target_label else "🕘 ประวัติงาน"
    if not entries:
        return f"{head}\nยังไม่มีประวัติการเปลี่ยนแปลง"
    lines = [head, *(f"• {entry_line(e)}" for e in entries)]
    lines.append("(💬 LINE / 🎮 Discord = แก้ผ่านแชท · 🌐 Plane = แก้ในเว็บ)")
    return "\n".join(lines)


def not_available_text() -> str:
    return "ดูประวัติไม่ได้ในขณะนี้"
