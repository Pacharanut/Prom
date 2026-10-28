"""Thai copy for the daily summary and the notification-settings panel (Phase 2B), shared by
LINE, Discord, and the scheduled push (the Celery task injects `compose_daily_summary` into
CheckDueDateNotifications). Text only — buttons are added by each channel module.
"""

from __future__ import annotations

from src.application.notifications.preferences import NotificationPreferenceView
from src.application.notifications.summary import DailySummary, SummaryItem
from src.channels.ai_copy import format_date
from src.domain.notifications.entities import NotificationEventType

EVENT_LABELS: dict[NotificationEventType, str] = {
    NotificationEventType.DAILY_SUMMARY: "สรุปงานประจำวัน (09:00)",
    NotificationEventType.ASSIGNED: "ได้รับมอบหมายงาน",
    NotificationEventType.UNASSIGNED: "ถูกถอดออกจากงาน",
    NotificationEventType.STATE_CHANGED: "สถานะงานเปลี่ยน",
    NotificationEventType.COMPLETED: "งานเสร็จ",
    NotificationEventType.COMMENT: "ความคิดเห็นใหม่",
    NotificationEventType.DUE_SOON: "ใกล้ครบกำหนด (ล่วงหน้า 1 วัน)",
    NotificationEventType.DUE_TODAY: "ครบกำหนดวันนี้",
    NotificationEventType.OVERDUE: "เลยกำหนด",
}

SHORT_LABELS: dict[NotificationEventType, str] = {
    NotificationEventType.DAILY_SUMMARY: "สรุปรายวัน",
    NotificationEventType.ASSIGNED: "ได้รับงาน",
    NotificationEventType.UNASSIGNED: "ถูกถอดงาน",
    NotificationEventType.STATE_CHANGED: "สถานะเปลี่ยน",
    NotificationEventType.COMPLETED: "งานเสร็จ",
    NotificationEventType.COMMENT: "Comment",
    NotificationEventType.DUE_SOON: "ใกล้ครบกำหนด",
    NotificationEventType.DUE_TODAY: "ครบกำหนดวันนี้",
    NotificationEventType.OVERDUE: "เลยกำหนด",
}
"""≤ 20 characters each with the on/off mark — LINE's Quick Reply label limit."""


def toggle_label(view: NotificationPreferenceView) -> str:
    return f"{'🔔' if view.enabled else '🔕'} {SHORT_LABELS[view.event_type]}"


def preferences_text(views: list[NotificationPreferenceView]) -> str:
    lines = ["🔔 ตั้งค่าการแจ้งเตือน (แตะเพื่อเปิด/ปิด):"]
    for view in views:
        lines.append(f"{'✅' if view.enabled else '⛔'} {EVENT_LABELS[view.event_type]}")
    lines.append('เมื่อเปิด "สรุปงานประจำวัน" แจ้งเตือนวันครบกำหนดจะถูกรวมไว้ในข้อความสรุปเดียวตอน 09:00')
    return "\n".join(lines)


def _section(title: str, items: list[SummaryItem], *, show_due: bool) -> list[str]:
    if not items:
        return []
    lines = [f"{title} ({len(items)})"]
    for item in items:
        due = f" — ส่ง {format_date(item.due_date)}" if show_due and item.due_date else ""
        lines.append(f"• {item.label}: {item.name}{due}")
    return lines


def compose_daily_summary(summary: DailySummary) -> str:
    head = f"☀️ สรุปงานของคุณ {format_date(summary.day)}"
    if summary.is_empty:
        return f"{head}\nไม่มีงานค้างที่ต้องติดตามวันนี้ 🎉 (งานที่เปิดอยู่ {summary.open_count} งาน)"
    blocks = [
        _section("🔴 เลยกำหนด", summary.overdue, show_due=True),
        _section("🟠 ครบกำหนดวันนี้", summary.due_today, show_due=False),
        _section("🟡 ครบกำหนดพรุ่งนี้", summary.due_soon, show_due=False),
        _section("🆕 ได้รับมอบหมายใน 24 ชม.", summary.recently_assigned, show_due=True),
        _section("⛔ ติดขัด (รองานอื่นอยู่)", summary.blocked, show_due=True),
    ]
    lines = [head, f"งานที่เปิดอยู่ทั้งหมด {summary.open_count} งาน"]
    for block in blocks:
        if block:
            lines.append("")
            lines.extend(block)
    if summary.truncated:
        lines.append("")
        lines.append("(นับจาก 10 Project แรกเท่านั้น)")
    lines.append("")
    lines.append('พิมพ์ "ตั้งค่าแจ้งเตือน" เพื่อเปิด/ปิดการแจ้งเตือนแต่ละแบบ')
    return "\n".join(lines)
