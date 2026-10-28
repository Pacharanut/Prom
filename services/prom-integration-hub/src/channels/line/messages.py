"""Builds LINE message JSON (plain dicts, sent as-is to reply/push). Quick Reply is used
throughout rather than hand-rolled Flex bubbles — LINE caps Quick Reply at 13 items / 20-char
labels, which comfortably fits every picker in this gate given the existing Gate 4/5 caps (10
projects, etc.), and is far less error-prone to construct correctly than Flex carousel JSON for a
Phase 1 scope with no visual-design requirement.
"""

from __future__ import annotations

from src.application.ai.replies import (
    AssistantReply,
    ChoiceReply,
    ConfirmReply,
    DoneReply,
    FallbackReply,
    NoticeReply,
    ShortcutReply,
    TimelineReply,
)
from src.application.notifications.preferences import NotificationPreferenceView
from src.application.notifications.summary import DailySummary
from src.application.prom_api.ports import PromUserProfile
from src.application.work_items.timeline import TimelineEntry
from src.channels import ai_copy, group_copy, notification_copy, timeline_copy
from src.channels.line.postback import encode_postback
from src.domain.groups.entities import GroupBinding, GroupEventType
from src.domain.work_items.entities import (
    ProjectMemberRef,
    ProjectRef,
    WorkItem,
    WorkItemState,
)
from src.domain.workspaces.entities import WorkspaceConnection

_LABEL_MAX_LEN = 20
_QUICK_REPLY_MAX_ITEMS = 13


def _truncate_label(text: str) -> str:
    return text if len(text) <= _LABEL_MAX_LEN else text[: _LABEL_MAX_LEN - 1] + "…"


def _quick_reply_item(label: str, data: str, display_text: str | None = None) -> dict[str, object]:
    return {
        "type": "action",
        "action": {
            "type": "postback",
            "label": _truncate_label(label),
            "data": data,
            "displayText": display_text or label,
        },
    }


def _text_with_quick_reply(text: str, items: list[dict[str, object]]) -> dict[str, object]:
    message: dict[str, object] = {"type": "text", "text": text}
    if items:
        message["quickReply"] = {"items": items[:_QUICK_REPLY_MAX_ITEMS]}
    return message


def build_menu_message() -> dict[str, object]:
    items = [
        _quick_reply_item("สร้างงาน", encode_postback("create")),
        _quick_reply_item("งานของฉัน", encode_postback("my_tasks")),
        _quick_reply_item("อัปเดต", encode_postback("update")),
        _quick_reply_item("สรุปงาน", encode_postback("summary")),
        _quick_reply_item("ตั้งค่าแจ้งเตือน", encode_postback("notif_settings")),
    ]
    return _text_with_quick_reply(
        "สวัสดีค่ะ 👋 เลือกเมนูที่ต้องการได้เลย:\nA. สร้างงาน\nB. งานของฉัน\nC. อัปเดต"
        "\nD. สรุปงาน\nE. ตั้งค่าแจ้งเตือน",
        items,
    )


def build_workspace_picker(workspaces: list[WorkspaceConnection]) -> dict[str, object]:
    items = [
        _quick_reply_item(ws.name, encode_postback("select_workspace", value=str(ws.id)))
        for ws in workspaces
    ]
    return _text_with_quick_reply("เลือก Workspace ที่ต้องการใช้งาน:", items)


def build_title_prompt() -> dict[str, object]:
    return {"type": "text", "text": "ตั้งชื่องาน (พิมพ์ 'ยกเลิก' เพื่อยกเลิก):"}


def build_project_picker(projects: list[ProjectRef]) -> dict[str, object]:
    items = [
        _quick_reply_item(p.name, encode_postback("select_project", value=p.id)) for p in projects
    ]
    return _text_with_quick_reply("เลือก Project:", items)


def build_state_picker(states: list[WorkItemState]) -> dict[str, object]:
    items = [_quick_reply_item("ข้าม (ค่าเริ่มต้น)", encode_postback("select_state", value="skip"))]
    items += [
        _quick_reply_item(s.name, encode_postback("select_state", value=s.id)) for s in states
    ]
    return _text_with_quick_reply("เลือกสถานะ (หรือข้ามเพื่อใช้ค่าเริ่มต้น):", items)


def build_assignee_picker(members: list[ProjectMemberRef]) -> dict[str, object]:
    items = [_quick_reply_item("ข้าม (ไม่ระบุ)", encode_postback("select_assignee", value="skip"))]
    items += [
        _quick_reply_item(m.display_name, encode_postback("select_assignee", value=m.user_id))
        for m in members
    ]
    return _text_with_quick_reply("มอบหมายให้ใคร (หรือข้าม):", items)


def build_due_date_prompt() -> dict[str, object]:
    return {"type": "text", "text": "กำหนดส่ง (YYYY-MM-DD) หรือพิมพ์ 'ข้าม':"}


def build_review_card(
    *,
    title: str,
    project_name: str,
    state_name: str | None,
    assignee_name: str | None,
    due_date: str | None,
    labels: list[str] | None = None,
    priority: str | None = None,
    tag_source: str | None = None,
) -> dict[str, object]:
    lines = [
        "ตรวจสอบข้อมูลก่อนสร้างงาน:",
        f"ชื่องาน: {title}",
        f"Project: {project_name}",
        f"สถานะ: {state_name or '(ค่าเริ่มต้น)'}",
        f"ผู้รับผิดชอบ: {assignee_name or '(ไม่ระบุ)'}",
        f"กำหนดส่ง: {due_date or '(ไม่ระบุ)'}",
        *ai_copy.tagging_lines(labels or [], priority, tag_source, priority_is_explicit=False),
    ]
    items = [_quick_reply_item("ยืนยัน", encode_postback("confirm"))]
    if labels or priority:
        items.append(
            _quick_reply_item("ยืนยัน (ไม่ใส่ tag)", encode_postback("confirm", value="plain"))
        )
    items.append(_quick_reply_item("ยกเลิก", encode_postback("cancel")))
    return _text_with_quick_reply("\n".join(lines), items)


def build_create_success_message(
    *, work_item: WorkItem, project_identifier: str, web_url: str, was_replay: bool
) -> dict[str, object]:
    identifier = f"{project_identifier}-{work_item.sequence_id}"
    prefix = "งานนี้ถูกสร้างไปแล้ว " if was_replay else "✅ สร้างงานสำเร็จ "
    return {"type": "text", "text": f"{prefix}{identifier}\n{web_url}"}


def build_cancelled_message() -> dict[str, object]:
    return {"type": "text", "text": "ยกเลิกแล้ว กลับสู่เมนูหลัก — พิมพ์ 'เมนู' เพื่อเริ่มใหม่"}


def build_linking_success_message(
    profile: PromUserProfile, *, has_multiple_workspaces: bool
) -> dict[str, object]:
    text = f"✅ เชื่อมต่อบัญชีสำเร็จ! ยินดีต้อนรับ {profile.display_name} ({profile.email})"
    if has_multiple_workspaces:
        text += "\n(บัญชีนี้เชื่อมได้หลาย Workspace — ระบบจะถามให้เลือกทุกครั้งที่ยังไม่ได้ตั้งค่าเริ่มต้น)"
    text += "\nพิมพ์ 'เมนู' เพื่อเริ่มใช้งาน"
    return {"type": "text", "text": text}


_ERROR_MESSAGES: dict[str, str] = {
    "LINKING_CODE_INVALID": "รหัสไม่ถูกต้อง กรุณาตรวจสอบและลองอีกครั้ง",
    "LINKING_CODE_EXPIRED": "รหัสหมดอายุแล้ว กรุณาสร้างรหัสใหม่จาก Prom",
    "RATE_LIMITED": "ลองมากเกินไป กรุณารอสักครู่แล้วลองใหม่",
    "IDENTITY_NOT_LINKED": "บัญชีนี้ยังไม่ได้เชื่อมต่อ — เชื่อมต่อก่อนใช้งานเมนูนี้",
    "WORKSPACE_NOT_ALLOWED": "Workspace นี้ไม่ได้เชื่อมกับบัญชี LINE นี้",
    "INTEGRATION_INACTIVE": "การเชื่อมต่อนี้ถูกปิดใช้งานแล้ว",
    "PROM_PERMISSION_DENIED": "คุณไม่มีสิทธิ์ทำรายการนี้ใน Prom",
    "WORK_ITEM_NOT_FOUND": "ไม่พบงานนี้",
    "PROM_API_UNAVAILABLE": "เชื่อมต่อ Prom ไม่ได้ในขณะนี้ กรุณาลองใหม่ภายหลัง",
    "PROM_REQUEST_INVALID": "ไม่สามารถทำรายการนี้ได้ กรุณาตรวจสอบข้อมูล",
}


def build_error_message(error_code: str) -> dict[str, object]:
    text = _ERROR_MESSAGES.get(error_code, "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง")
    return {"type": "text", "text": f"❌ {text}"}


def build_my_tasks_list(
    items: list[WorkItem], projects_by_id: dict[str, ProjectRef], *, truncated: bool
) -> dict[str, object]:
    if not items:
        return {"type": "text", "text": "ไม่พบงานที่ได้รับมอบหมาย"}

    lines = ["งานของฉัน:"]
    quick_reply_items: list[dict[str, object]] = []
    for item in items[:_QUICK_REPLY_MAX_ITEMS]:
        project = projects_by_id.get(item.project_id)
        identifier = (
            f"{project.identifier}-{item.sequence_id}" if project else str(item.sequence_id)
        )
        lines.append(f"• {identifier}: {item.name}")
        quick_reply_items.append(
            _quick_reply_item(
                identifier,
                encode_postback("select_work_item", value=item.id, project=item.project_id),
            )
        )
    if truncated:
        lines.append("(แสดงผลจาก 10 Project แรกเท่านั้น)")
    lines.append("แตะปุ่มด้านล่างเพื่อจัดการงาน:")
    return _text_with_quick_reply("\n".join(lines), quick_reply_items)


def build_update_action_menu() -> dict[str, object]:
    items = [
        _quick_reply_item("เปลี่ยนสถานะ", encode_postback("select_update_action", value="state")),
        _quick_reply_item("เพิ่ม Comment", encode_postback("select_update_action", value="comment")),
        _quick_reply_item("Assign ใหม่", encode_postback("select_update_action", value="assignee")),
        _quick_reply_item(
            "Mark Complete", encode_postback("select_update_action", value="complete")
        ),
        _quick_reply_item("ประวัติ", encode_postback("select_update_action", value="history")),
        _quick_reply_item("ยกเลิก", encode_postback("cancel")),
    ]
    return _text_with_quick_reply("เลือกการอัปเดต:", items)


def build_timeline_message(
    target_label: str | None, entries: list[TimelineEntry]
) -> dict[str, object]:
    """Phase 2E: the history, with the update menu still attached so the user can carry on."""
    menu = build_update_action_menu()
    menu["text"] = timeline_copy.timeline_text(target_label, entries) + "\n\nเลือกการอัปเดต:"
    return menu


def build_comment_prompt() -> dict[str, object]:
    return {"type": "text", "text": "พิมพ์ข้อความ Comment:"}


def build_confirm_complete_prompt() -> dict[str, object]:
    items = [
        _quick_reply_item("ยืนยัน", encode_postback("confirm")),
        _quick_reply_item("ยกเลิก", encode_postback("cancel")),
    ]
    return _text_with_quick_reply("ยืนยันการทำเครื่องหมายว่าเสร็จสิ้น?", items)


def build_update_success_message(*, action: str) -> dict[str, object]:
    labels = {
        "state": "อัปเดตสถานะสำเร็จ",
        "assignee": "มอบหมายงานใหม่สำเร็จ",
        "comment": "เพิ่ม Comment สำเร็จ",
        "complete": "ทำเครื่องหมายว่าเสร็จสิ้นสำเร็จ",
    }
    return {"type": "text", "text": f"✅ {labels.get(action, 'อัปเดตสำเร็จ')}"}


# --- AI assistant (Phase 2) -------------------------------------------------------------------


def build_ai_reply(reply: AssistantReply) -> dict[str, object]:
    """Renders any AiAssistant reply as one LINE message. Copy comes from ai_copy.py (shared
    with Discord); this function only adds the Quick Reply buttons."""
    if isinstance(reply, ShortcutReply):
        return build_menu_message()  # handlers open the panel themselves; never reached normally
    if isinstance(reply, TimelineReply):
        return {
            "type": "text",
            "text": timeline_copy.timeline_text(reply.target_label, reply.entries),
        }
    if isinstance(reply, FallbackReply):
        menu = build_menu_message()
        hint = ai_copy.fallback_text(reply)
        if hint:
            menu["text"] = f"{hint}\n\n{menu['text']}"
        return menu
    if isinstance(reply, NoticeReply):
        return {"type": "text", "text": ai_copy.notice_text(reply.notice)}
    if isinstance(reply, DoneReply):
        return {"type": "text", "text": ai_copy.done_text(reply)}
    if isinstance(reply, ConfirmReply):
        items = [_quick_reply_item("ยืนยัน", encode_postback("ai_confirm"))]
        if reply.can_skip_suggestions:
            items.append(
                _quick_reply_item("ยืนยัน (ไม่ใส่ tag)", encode_postback("ai_confirm", value="plain"))
            )
        items.append(_quick_reply_item("ยกเลิก", encode_postback("cancel")))
        return _text_with_quick_reply(ai_copy.confirm_text(reply), items)
    if isinstance(reply, ChoiceReply):
        items = [
            _quick_reply_item(option.label, encode_postback("ai_pick", value=option.value))
            for option in reply.options[: _QUICK_REPLY_MAX_ITEMS - 1]
        ]
        items.append(_quick_reply_item("ยกเลิก", encode_postback("cancel")))
        return _text_with_quick_reply(ai_copy.choice_prompt(reply.kind, reply.subject), items)
    rows = reply.rows[:_QUICK_REPLY_MAX_ITEMS]
    items = [
        _quick_reply_item(
            row.label,
            encode_postback("select_work_item", value=row.work_item_id, project=row.project_id),
        )
        for row in rows
    ]
    return _text_with_quick_reply(ai_copy.list_text(reply, max_rows=_QUICK_REPLY_MAX_ITEMS), items)


# --- Notifications (Phase 2B) -----------------------------------------------------------------


def build_preferences_panel(views: list[NotificationPreferenceView]) -> dict[str, object]:
    items = [
        _quick_reply_item(
            notification_copy.toggle_label(view),
            encode_postback("notif_toggle", value=view.event_type.value),
        )
        for view in views
    ]
    return _text_with_quick_reply(notification_copy.preferences_text(views), items)


def build_summary_message(summary: DailySummary) -> dict[str, object]:
    return {"type": "text", "text": notification_copy.compose_daily_summary(summary)}


# --- Groups (Phase 2D) ------------------------------------------------------------------------


def build_text(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def build_group_workspace_picker(workspaces: list[WorkspaceConnection]) -> dict[str, object]:
    items = [
        _quick_reply_item(ws.name, encode_postback("group_ws", value=str(ws.id)))
        for ws in workspaces[:_QUICK_REPLY_MAX_ITEMS]
    ]
    return _text_with_quick_reply(group_copy.choose_workspace_text(), items)


def build_group_project_picker(
    projects: list[ProjectRef], workspace_connection_id: str
) -> dict[str, object]:
    items = [
        _quick_reply_item(
            group_copy.ALL_PROJECTS_LABEL,
            encode_postback("group_bind", value="all", ws=workspace_connection_id),
        )
    ]
    items += [
        _quick_reply_item(
            p.name, encode_postback("group_bind", value=p.id, ws=workspace_connection_id)
        )
        for p in projects
    ]
    return _text_with_quick_reply(group_copy.choose_project_text(), items)


def build_group_settings(binding: GroupBinding, *, just_bound: bool = False) -> dict[str, object]:
    items = [
        _quick_reply_item(
            group_copy.toggle_label(binding, event_type),
            encode_postback("group_toggle", value=event_type.value),
        )
        for event_type in GroupEventType
    ]
    items.append(_quick_reply_item("ยกเลิกผูกกลุ่ม", encode_postback("group_unbind")))
    text = group_copy.bound_text(binding) if just_bound else group_copy.settings_text(binding)
    return _text_with_quick_reply(text, items)
