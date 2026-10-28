"""User-facing Thai copy for the AI assistant's structured replies (src/application/ai/
replies.py), shared by the LINE and Discord presenters so both channels say the same thing.
Buttons/components are added by each channel's own module; this file is text only.
"""

from __future__ import annotations

from datetime import date

from src.application.ai.replies import (
    ConfirmReply,
    DoneReply,
    FallbackReply,
    ListReply,
    Notice,
)

_THAI_MONTHS = (
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
_THAI_WEEKDAYS = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์")


def format_date(value: date | None) -> str:
    """ "ศุกร์ 30 ต.ค. 2026" — weekday included so a wrong reading of "วันศุกร์" is obvious."""
    if value is None:
        return "(ไม่ระบุ)"
    return f"{_THAI_WEEKDAYS[value.weekday()]} {value.day} {_THAI_MONTHS[value.month - 1]} {value.year}"


_NOTICE_TEXT: dict[str, str] = {
    "need_title": 'ยังไม่รู้ชื่องาน ลองพิมพ์ใหม่ เช่น "สร้างงาน Deploy AI Platform ส่งวันศุกร์"',
    "no_projects": "ยังไม่มี Project ที่คุณเข้าถึงได้ใน Workspace นี้",
    "project_not_found": 'ไม่พบ Project "{detail}"',
    "target_not_found": 'ไม่พบงานที่ชื่อ "{detail}" ลองระบุชื่อให้ชัดขึ้น หรือใช้รหัสงาน เช่น PROM-12',
    "need_target": 'ไม่แน่ใจว่าหมายถึงงานไหน ระบุชื่องานหรือรหัสงาน เช่น "task API Gateway เสร็จแล้ว"',
    "member_not_found": 'ไม่พบสมาชิกชื่อ "{detail}" ใน Project นี้',
    "member_ambiguous": "พบหลายคนที่ชื่อใกล้เคียง: {detail} — ระบุชื่อให้ชัดขึ้น",
    "date_unreadable": 'อ่านวันที่ "{detail}" ไม่ออก ลองใช้ เช่น พรุ่งนี้, วันศุกร์, สิ้นเดือน, อีก 2 วัน หรือ 2026-11-05',
    "need_comment": 'ยังไม่มีข้อความ Comment ลองพิมพ์ เช่น "comment งาน X ว่า รอ review"',
    "state_not_supported": 'ตอนนี้เปลี่ยนสถานะผ่าน AI ได้แค่ "เริ่มทำ" และ "เสร็จแล้ว" — สถานะอื่นใช้เมนู อัปเดต',
    "no_state_in_group": 'Project นี้ไม่มีสถานะ "กำลังทำ" ให้เปลี่ยนไป',
    "need_search_criteria": 'ระบุสิ่งที่จะค้นหา เช่น "งาน infra", "งานของ project AI" หรือ "งานของ Nok"',
    "rate_limited": "ส่งคำสั่งถี่เกินไป กรุณารอสักครู่แล้วลองใหม่",
    "session_expired": "รายการนี้หมดอายุแล้ว พิมพ์คำสั่งใหม่ได้เลย",
}

_ERROR_TEXT: dict[str, str] = {
    "PROM_PERMISSION_DENIED": "คุณไม่มีสิทธิ์ทำรายการนี้ใน Prom",
    "WORK_ITEM_NOT_FOUND": "ไม่พบงานนี้",
    "PROM_API_UNAVAILABLE": "เชื่อมต่อ Prom ไม่ได้ในขณะนี้ กรุณาลองใหม่ภายหลัง",
    "PROM_REQUEST_INVALID": "ไม่สามารถทำรายการนี้ได้ กรุณาตรวจสอบข้อมูล",
    "IDENTITY_NOT_LINKED": "บัญชีนี้ยังไม่ได้เชื่อมต่อกับ Prom",
}


PRIORITY_LABELS: dict[str, str] = {
    "urgent": "ด่วนมาก",
    "high": "สูง",
    "medium": "กลาง",
    "low": "ต่ำ",
    "none": "ไม่ระบุ",
}


def tagging_lines(
    labels: list[str], priority: str | None, source: str | None, *, priority_is_explicit: bool
) -> list[str]:
    """Phase 2C: the Label/Priority lines of a create confirmation, with where they came from."""
    origin = {"rule": " (ตามกฎของ workspace)", "ai": " (AI แนะนำ)"}.get(source or "", "")
    lines: list[str] = []
    if labels:
        lines.append(f"Label: {', '.join(labels)}{origin}")
    if priority:
        priority_origin = " (ตามที่ระบุ)" if priority_is_explicit else origin
        lines.append(f"Priority: {PRIORITY_LABELS.get(priority, priority)}{priority_origin}")
    return lines


def notice_text(notice: Notice) -> str:
    if notice.code == "error":
        return "❌ " + _ERROR_TEXT.get(notice.detail or "", "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง")
    template = _NOTICE_TEXT.get(notice.code, "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง")
    return template.format(detail=notice.detail or "")


def fallback_text(reply: FallbackReply) -> str | None:
    """Shown above the normal menu. None means "just show the menu"."""
    if reply.reason == "unavailable":
        return "🤖 ผู้ช่วย AI ไม่พร้อมใช้งานตอนนี้ ใช้เมนูด้านล่างแทนได้เลย"
    if reply.reason == "not_understood":
        return '🤖 ยังไม่เข้าใจคำสั่งนี้ ลองพิมพ์ใหม่ เช่น "งานของฉันวันนี้" หรือเลือกจากเมนู'
    return None


def confirm_text(reply: ConfirmReply) -> str:
    lines: list[str]
    if reply.action == "create":
        lines = [
            "🤖 ตรวจสอบก่อนสร้างงาน:",
            f"ชื่องาน: {reply.title}",
            f"Project: {reply.project_name}",
            f"สถานะ: {reply.state_name or '(ค่าเริ่มต้น)'}",
            f"ผู้รับผิดชอบ: {reply.assignee_name or '(ไม่ระบุ)'}",
            f"กำหนดส่ง: {format_date(reply.due_date)}",
            *tagging_lines(
                reply.labels,
                reply.priority,
                reply.tag_source,
                priority_is_explicit=reply.priority_is_explicit,
            ),
        ]
    elif reply.action == "complete":
        lines = ["🤖 ยืนยันปิดงานนี้ว่าเสร็จแล้ว?", f"งาน: {reply.target_label}"]
    elif reply.action == "start":
        lines = [
            "🤖 ยืนยันเปลี่ยนสถานะ?",
            f"งาน: {reply.target_label}",
            f"สถานะใหม่: {reply.state_name}",
        ]
    elif reply.action == "reassign":
        lines = [
            "🤖 ยืนยันมอบหมายงานใหม่?",
            f"งาน: {reply.target_label}",
            f"ผู้รับผิดชอบใหม่: {reply.assignee_name}",
        ]
    elif reply.action == "reschedule":
        lines = [
            "🤖 ยืนยันเลื่อนกำหนดส่ง?",
            f"งาน: {reply.target_label}",
            f"จาก: {format_date(reply.old_due_date)}",
            f"เป็น: {format_date(reply.due_date)}",
        ]
    else:
        lines = ["🤖 ยืนยันเพิ่ม Comment?", f"งาน: {reply.target_label}", f"ข้อความ: {reply.comment}"]
    for warning in reply.warnings:
        lines.append("⚠️ " + notice_text(warning))
    return "\n".join(lines)


_DONE_TEXT: dict[str, str] = {
    "create": "✅ สร้างงานสำเร็จ",
    "complete": "✅ ปิดงานเรียบร้อย",
    "start": "✅ เปลี่ยนสถานะเรียบร้อย",
    "reassign": "✅ มอบหมายงานใหม่เรียบร้อย",
    "reschedule": "✅ เลื่อนกำหนดส่งเรียบร้อย",
    "comment": "✅ เพิ่ม Comment เรียบร้อย",
}


def done_text(reply: DoneReply) -> str:
    head = "งานนี้ถูกสร้างไปแล้ว" if reply.was_replay else _DONE_TEXT[reply.action]
    text = f"{head}\n{reply.target_label}"
    if reply.web_url:
        text += f"\n{reply.web_url}"
    return text


_LIST_TITLES: dict[str, str] = {
    "assigned": "งานของฉัน",
    "due_today": "งานที่ครบกำหนดวันนี้",
    "overdue": "งานที่เลยกำหนด",
    "due_soon": "งานที่ใกล้ครบกำหนด",
    "search": 'ผลการค้นหา "{subject}"',
    "project": "งานล่าสุดใน Project {subject}",
    "person": "งานของ {subject}",
}

_LIST_EMPTY: dict[str, str] = {
    "assigned": "ไม่พบงานที่ได้รับมอบหมาย 🎉",
    "due_today": "ไม่มีงานที่ครบกำหนดวันนี้ 🎉",
    "overdue": "ไม่มีงานที่เลยกำหนด 🎉",
    "due_soon": "ไม่มีงานที่ใกล้ครบกำหนด",
    "search": 'ไม่พบงานที่ตรงกับ "{subject}"',
    "project": "ยังไม่มีงานใน Project {subject}",
    "person": "{subject} ยังไม่มีงานที่ได้รับมอบหมาย",
}


def list_text(reply: ListReply, *, max_rows: int) -> str:
    subject = reply.subject or ""
    if not reply.rows:
        return _LIST_EMPTY[reply.kind].format(subject=subject)
    lines = [_LIST_TITLES[reply.kind].format(subject=subject) + ":"]
    for row in reply.rows[:max_rows]:
        due = f" (ส่ง {format_date(row.due_date)})" if row.due_date else ""
        lines.append(f"• {row.label}: {row.name}{due}")
    if reply.truncated:
        lines.append("(แสดงผลจากบาง Project เท่านั้น)")
    lines.append('แตะรหัสงานด้านล่างเพื่อจัดการ หรือพิมพ์คำสั่งต่อได้เลย เช่น "mark complete"')
    return "\n".join(lines)


def choice_prompt(kind: str, subject: str | None) -> str:
    if kind == "project":
        return f'🤖 จะสร้างงาน "{subject}" ใน Project ไหน?'
    return f'🤖 พบหลายงานที่ตรงกับ "{subject}" — หมายถึงงานไหน?'
