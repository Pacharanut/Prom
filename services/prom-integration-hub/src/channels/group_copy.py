"""Thai copy for group features (Phase 2D), shared by LINE and Discord, plus the event message
composer injected into GroupNotifier."""

from __future__ import annotations

from src.application.groups.notifier import GroupEvent
from src.domain.groups.entities import GroupBinding, GroupEventType

EVENT_LABELS: dict[GroupEventType, str] = {
    GroupEventType.CREATED: "มีงานใหม่",
    GroupEventType.ASSIGNED: "มอบหมายงาน",
    GroupEventType.COMPLETED: "งานเสร็จ",
}

BIND_KEYWORDS = {"ผูกกลุ่ม", "bind group", "/bind"}
SETTINGS_KEYWORDS = {"ตั้งค่ากลุ่ม", "group settings", "/settings"}
UNBIND_KEYWORDS = {"ยกเลิกผูกกลุ่ม", "unbind group", "/unbind"}
HELP_KEYWORDS = {"วิธีใช้กลุ่ม", "group help", "/help"}


def _scope(binding: GroupBinding) -> str:
    return f"Project {binding.project_name}" if binding.project_name else "ทุก Project ใน Workspace"


def join_text() -> str:
    return (
        "สวัสดีค่ะ 👋 บอท Prom แจ้งความเคลื่อนไหวของงานในกลุ่มนี้ได้\n"
        '• พิมพ์ "ผูกกลุ่ม" เพื่อเลือก Project ที่จะติดตาม\n'
        '• พิมพ์ "ตั้งค่ากลุ่ม" เพื่อเลือกว่าจะแจ้งเรื่องอะไร\n'
        '• พิมพ์ "ยกเลิกผูกกลุ่ม" เพื่อหยุดแจ้ง\n'
        "ผู้สั่งต้องผูกบัญชี Prom กับบอทนี้ในแชทส่วนตัวก่อน · บอทไม่ตอบข้อความอื่นในกลุ่ม"
    )


def not_linked_text() -> str:
    return "ต้องผูกบัญชี Prom ของคุณกับบอทนี้ก่อน (แชทส่วนตัวกับบอท → พิมพ์รหัสจากหน้า Connected Accounts)"


def no_workspace_text() -> str:
    return "บัญชีนี้ยังไม่ได้เชื่อมกับ Workspace ใดเลย ติดต่อผู้ดูแลระบบ"


def choose_workspace_text() -> str:
    return "เลือก Workspace ที่จะผูกกับกลุ่มนี้:"


def choose_project_text() -> str:
    return "เลือก Project ที่จะให้แจ้งในกลุ่มนี้:"


ALL_PROJECTS_LABEL = "ทุก Project"


def not_bound_text() -> str:
    return 'กลุ่มนี้ยังไม่ได้ผูกกับ Project ไหน — พิมพ์ "ผูกกลุ่ม" เพื่อเริ่ม'


def settings_text(binding: GroupBinding) -> str:
    lines = [f"⚙️ กลุ่มนี้ติดตาม: {_scope(binding)}", "แจ้งเมื่อ (แตะเพื่อเปิด/ปิด):"]
    for event_type, label in EVENT_LABELS.items():
        lines.append(f"{'✅' if event_type in binding.events else '⛔'} {label}")
    return "\n".join(lines)


def toggle_label(binding: GroupBinding, event_type: GroupEventType) -> str:
    return f"{'🔔' if event_type in binding.events else '🔕'} {EVENT_LABELS[event_type]}"


def bound_text(binding: GroupBinding) -> str:
    return f"✅ ผูกกลุ่มนี้กับ {_scope(binding)} แล้ว\n\n" + settings_text(binding)


def unbound_text() -> str:
    return "ยกเลิกการผูกกลุ่มแล้ว บอทจะไม่แจ้งความเคลื่อนไหวในกลุ่มนี้อีก"


def project_missing_text() -> str:
    return 'ไม่พบ Project นี้ในบัญชีของคุณ ลองพิมพ์ "ผูกกลุ่ม" ใหม่'


def compose_group_event(event: GroupEvent, binding: GroupBinding) -> str:
    ref = f"#{event.sequence_id} {event.name}" if event.sequence_id else event.name
    by = f"\nโดย {event.actor_name}" if event.actor_name else ""
    scope = f" · {binding.project_name}" if binding.project_name else ""
    if event.event_type is GroupEventType.COMPLETED:
        return f"✅ งานเสร็จแล้ว: {ref}{by}{scope}"
    if event.event_type is GroupEventType.ASSIGNED:
        who = ", ".join(event.assignee_names)
        return f"👤 มอบหมายงาน {ref} ให้ {who}{by}{scope}"
    assigned = f" (มอบหมาย: {', '.join(event.assignee_names)})" if event.assignee_names else ""
    return f"🆕 งานใหม่: {ref}{assigned}{by}{scope}"
