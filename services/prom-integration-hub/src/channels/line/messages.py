"""Builds LINE message JSON (plain dicts, sent as-is to reply/push). Quick Reply is used
throughout rather than hand-rolled Flex bubbles — LINE caps Quick Reply at 13 items / 20-char
labels, which comfortably fits every picker in this gate given the existing Gate 4/5 caps (10
projects, etc.), and is far less error-prone to construct correctly than Flex carousel JSON for a
Phase 1 scope with no visual-design requirement.
"""

from __future__ import annotations

from src.application.prom_api.ports import PromUserProfile
from src.channels.line.postback import encode_postback
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
    ]
    return _text_with_quick_reply(
        "สวัสดีค่ะ 👋 เลือกเมนูที่ต้องการได้เลย:\nA. สร้างงาน\nB. งานของฉัน\nC. อัปเดต", items
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
) -> dict[str, object]:
    lines = [
        "ตรวจสอบข้อมูลก่อนสร้างงาน:",
        f"ชื่องาน: {title}",
        f"Project: {project_name}",
        f"สถานะ: {state_name or '(ค่าเริ่มต้น)'}",
        f"ผู้รับผิดชอบ: {assignee_name or '(ไม่ระบุ)'}",
        f"กำหนดส่ง: {due_date or '(ไม่ระบุ)'}",
    ]
    items = [
        _quick_reply_item("ยืนยัน", encode_postback("confirm")),
        _quick_reply_item("ยกเลิก", encode_postback("cancel")),
    ]
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
        _quick_reply_item("ยกเลิก", encode_postback("cancel")),
    ]
    return _text_with_quick_reply("เลือกการอัปเดต:", items)


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
