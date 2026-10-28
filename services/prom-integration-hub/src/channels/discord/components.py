"""Builds plain, framework-free message/component descriptors for Discord — the Discord
counterpart to `src/channels/line/messages.py`. Returns `DiscordMessage` dataclasses, not real
`discord.py` `View`/`Button`/`Select` objects, so every builder here is unit-testable without a
live Gateway connection or any discord.py runtime state; `src/channels/discord/client.py` is the
only place that converts a `DiscordMessage` into a real `discord.ui.View`.

Deliberate divergence from LINE's design, not a mirror-for-mirror's-sake port: LINE has no native
"pick one of N" UI primitive, so Gate 5 uses Quick Reply buttons for every choice (project, state,
assignee, ...). Discord has a native Select Menu component (up to 25 options) that is the more
idiomatic fit for those same pickers, so this module uses Selects for "pick one of N" and Buttons
only for fixed, parameterless actions (confirm/cancel/menu). Because every picker's chosen value
travels back via the interaction's own `values[0]` payload (not encoded into `custom_id`), and
every other piece of context the handler needs is already sitting in the stored
`ConversationState` (exactly like LINE's handler reads `state.data["project_id"]` rather than
re-deriving it from postback data), no `custom_id` here ever needs to carry more than a bare,
human-readable action name — unlike LINE's `postback.py`, there is no length-driven encoding
scheme needed at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
from src.domain.groups.entities import GroupBinding, GroupEventType
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem, WorkItemState
from src.domain.workspaces.entities import WorkspaceConnection

_SELECT_MAX_OPTIONS = 25
"""Discord's own Select Menu limit."""
_OPTION_LABEL_MAX_LEN = 100
_BUTTON_LABEL_MAX_LEN = 80


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


@dataclass
class ButtonSpec:
    label: str
    custom_id: str
    style: str = "primary"
    """One of "primary"|"secondary"|"danger" — mapped to discord.ButtonStyle by client.py."""


@dataclass
class SelectOptionSpec:
    label: str
    value: str


@dataclass
class SelectSpec:
    custom_id: str
    placeholder: str
    options: list[SelectOptionSpec]


@dataclass
class DiscordMessage:
    content: str
    buttons: list[ButtonSpec] = field(default_factory=list)
    select: SelectSpec | None = None


def _select(custom_id: str, placeholder: str, options: list[SelectOptionSpec]) -> SelectSpec:
    return SelectSpec(
        custom_id=custom_id, placeholder=placeholder, options=options[:_SELECT_MAX_OPTIONS]
    )


def build_menu_message() -> DiscordMessage:
    return DiscordMessage(
        content="สวัสดีค่ะ 👋 เลือกเมนูที่ต้องการได้เลย:",
        buttons=[
            ButtonSpec(label="สร้างงาน", custom_id="menu_create"),
            ButtonSpec(label="งานของฉัน", custom_id="menu_my_tasks"),
            ButtonSpec(label="อัปเดต", custom_id="menu_update"),
            ButtonSpec(label="สรุปงาน", custom_id="menu_summary", style="secondary"),
            ButtonSpec(label="ตั้งค่าแจ้งเตือน", custom_id="menu_notif_settings", style="secondary"),
        ],
    )


def build_workspace_picker(workspaces: list[WorkspaceConnection]) -> DiscordMessage:
    options = [
        SelectOptionSpec(label=_truncate(ws.name, _OPTION_LABEL_MAX_LEN), value=str(ws.id))
        for ws in workspaces
    ]
    return DiscordMessage(
        content="เลือก Workspace ที่ต้องการใช้งาน:",
        select=_select("select_workspace", "เลือก Workspace", options),
    )


def build_title_prompt() -> DiscordMessage:
    return DiscordMessage(content="ตั้งชื่องาน (พิมพ์ 'ยกเลิก' เพื่อยกเลิก):")


def build_project_picker(projects: list[ProjectRef]) -> DiscordMessage:
    options = [
        SelectOptionSpec(label=_truncate(p.name, _OPTION_LABEL_MAX_LEN), value=p.id)
        for p in projects
    ]
    return DiscordMessage(
        content="เลือก Project:", select=_select("select_project", "เลือก Project", options)
    )


def build_state_picker(states: list[WorkItemState]) -> DiscordMessage:
    options = [SelectOptionSpec(label="ข้าม (ค่าเริ่มต้น)", value="skip")]
    options += [
        SelectOptionSpec(label=_truncate(s.name, _OPTION_LABEL_MAX_LEN), value=s.id) for s in states
    ]
    return DiscordMessage(
        content="เลือกสถานะ (หรือข้ามเพื่อใช้ค่าเริ่มต้น):",
        select=_select("select_state", "เลือกสถานะ", options),
    )


def build_assignee_picker(members: list[ProjectMemberRef]) -> DiscordMessage:
    options = [SelectOptionSpec(label="ข้าม (ไม่ระบุ)", value="skip")]
    options += [
        SelectOptionSpec(label=_truncate(m.display_name, _OPTION_LABEL_MAX_LEN), value=m.user_id)
        for m in members
    ]
    return DiscordMessage(
        content="มอบหมายให้ใคร (หรือข้าม):",
        select=_select("select_assignee", "เลือกผู้รับผิดชอบ", options),
    )


def build_due_date_prompt() -> DiscordMessage:
    return DiscordMessage(content="กำหนดส่ง (YYYY-MM-DD) หรือพิมพ์ 'ข้าม':")


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
) -> DiscordMessage:
    lines = [
        "ตรวจสอบข้อมูลก่อนสร้างงาน:",
        f"ชื่องาน: {title}",
        f"Project: {project_name}",
        f"สถานะ: {state_name or '(ค่าเริ่มต้น)'}",
        f"ผู้รับผิดชอบ: {assignee_name or '(ไม่ระบุ)'}",
        f"กำหนดส่ง: {due_date or '(ไม่ระบุ)'}",
        *ai_copy.tagging_lines(labels or [], priority, tag_source, priority_is_explicit=False),
    ]
    buttons = [ButtonSpec(label="ยืนยัน", custom_id="confirm", style="primary")]
    if labels or priority:
        buttons.append(
            ButtonSpec(label="ยืนยัน (ไม่ใส่ tag)", custom_id="confirm_plain", style="secondary")
        )
    buttons.append(ButtonSpec(label="ยกเลิก", custom_id="cancel", style="danger"))
    return DiscordMessage(content="\n".join(lines), buttons=buttons)


def build_create_success_message(
    *, work_item: WorkItem, project_identifier: str, web_url: str, was_replay: bool
) -> DiscordMessage:
    identifier = f"{project_identifier}-{work_item.sequence_id}"
    prefix = "งานนี้ถูกสร้างไปแล้ว " if was_replay else "✅ สร้างงานสำเร็จ "
    return DiscordMessage(content=f"{prefix}{identifier}\n{web_url}")


def build_cancelled_message() -> DiscordMessage:
    return DiscordMessage(content="ยกเลิกแล้ว กลับสู่เมนูหลัก — พิมพ์ 'เมนู' เพื่อเริ่มใหม่")


def build_linking_success_message(
    profile: PromUserProfile, *, has_multiple_workspaces: bool
) -> DiscordMessage:
    text = f"✅ เชื่อมต่อบัญชีสำเร็จ! ยินดีต้อนรับ {profile.display_name} ({profile.email})"
    if has_multiple_workspaces:
        text += "\n(บัญชีนี้เชื่อมได้หลาย Workspace — ระบบจะถามให้เลือกทุกครั้งที่ยังไม่ได้ตั้งค่าเริ่มต้น)"
    text += "\nพิมพ์ 'เมนู' เพื่อเริ่มใช้งาน"
    return DiscordMessage(content=text)


_ERROR_MESSAGES: dict[str, str] = {
    "LINKING_CODE_INVALID": "รหัสไม่ถูกต้อง กรุณาตรวจสอบและลองอีกครั้ง",
    "LINKING_CODE_EXPIRED": "รหัสหมดอายุแล้ว กรุณาสร้างรหัสใหม่จาก Prom",
    "RATE_LIMITED": "ลองมากเกินไป กรุณารอสักครู่แล้วลองใหม่",
    "IDENTITY_NOT_LINKED": "บัญชีนี้ยังไม่ได้เชื่อมต่อ — เชื่อมต่อก่อนใช้งานเมนูนี้",
    "WORKSPACE_NOT_ALLOWED": "Workspace นี้ไม่ได้เชื่อมกับบัญชี Discord นี้",
    "INTEGRATION_INACTIVE": "การเชื่อมต่อนี้ถูกปิดใช้งานแล้ว",
    "PROM_PERMISSION_DENIED": "คุณไม่มีสิทธิ์ทำรายการนี้ใน Prom",
    "WORK_ITEM_NOT_FOUND": "ไม่พบงานนี้",
    "PROM_API_UNAVAILABLE": "เชื่อมต่อ Prom ไม่ได้ในขณะนี้ กรุณาลองใหม่ภายหลัง",
    "PROM_REQUEST_INVALID": "ไม่สามารถทำรายการนี้ได้ กรุณาตรวจสอบข้อมูล",
}


def build_error_message(error_code: str) -> DiscordMessage:
    text = _ERROR_MESSAGES.get(error_code, "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง")
    return DiscordMessage(content=f"❌ {text}")


def build_my_tasks_list(
    items: list[WorkItem], projects_by_id: dict[str, ProjectRef], *, truncated: bool
) -> DiscordMessage:
    if not items:
        return DiscordMessage(content="ไม่พบงานที่ได้รับมอบหมาย")

    lines = ["งานของฉัน:"]
    options: list[SelectOptionSpec] = []
    for item in items[:_SELECT_MAX_OPTIONS]:
        project = projects_by_id.get(item.project_id)
        identifier = (
            f"{project.identifier}-{item.sequence_id}" if project else str(item.sequence_id)
        )
        lines.append(f"• {identifier}: {item.name}")
        options.append(
            SelectOptionSpec(
                label=_truncate(f"{identifier}: {item.name}", _OPTION_LABEL_MAX_LEN),
                # A Select option carries exactly one `value` field — unlike LINE's postback
                # `data` string, which could hold both `value=` and `project=` at once. The
                # handler needs both the work item id and its project id to enter the update
                # flow, so they're joined here and split on the other end
                # (DiscordWebhookHandler._handle_select_work_item). UUIDs never contain ":".
                value=f"{item.project_id}:{item.id}",
            )
        )
    if truncated:
        lines.append("(แสดงผลจาก 10 Project แรกเท่านั้น)")
    lines.append("เลือกงานด้านล่างเพื่อจัดการ:")
    return DiscordMessage(
        content="\n".join(lines), select=_select("select_work_item", "เลือกงาน", options)
    )


def build_update_action_menu() -> DiscordMessage:
    return DiscordMessage(
        content="เลือกการอัปเดต:",
        select=_select(
            "select_update_action",
            "เลือกการอัปเดต",
            [
                SelectOptionSpec(label="เปลี่ยนสถานะ", value="state"),
                SelectOptionSpec(label="เพิ่ม Comment", value="comment"),
                SelectOptionSpec(label="Assign ใหม่", value="assignee"),
                SelectOptionSpec(label="Mark Complete", value="complete"),
                SelectOptionSpec(label="ประวัติ", value="history"),
            ],
        ),
        buttons=[ButtonSpec(label="ยกเลิก", custom_id="cancel", style="danger")],
    )


def build_comment_prompt() -> DiscordMessage:
    return DiscordMessage(content="พิมพ์ข้อความ Comment:")


def build_confirm_complete_prompt() -> DiscordMessage:
    return DiscordMessage(
        content="ยืนยันการทำเครื่องหมายว่าเสร็จสิ้น?",
        buttons=[
            ButtonSpec(label="ยืนยัน", custom_id="confirm", style="primary"),
            ButtonSpec(label="ยกเลิก", custom_id="cancel", style="danger"),
        ],
    )


def build_update_success_message(*, action: str) -> DiscordMessage:
    labels = {
        "state": "อัปเดตสถานะสำเร็จ",
        "assignee": "มอบหมายงานใหม่สำเร็จ",
        "comment": "เพิ่ม Comment สำเร็จ",
        "complete": "ทำเครื่องหมายว่าเสร็จสิ้นสำเร็จ",
    }
    return DiscordMessage(content=f"✅ {labels.get(action, 'อัปเดตสำเร็จ')}")


# --- AI assistant (Phase 2) -------------------------------------------------------------------


def build_ai_reply(reply: AssistantReply) -> DiscordMessage:
    """Discord counterpart to line/messages.py's build_ai_reply — same copy (ai_copy.py),
    Discord-native components: Buttons for confirm/cancel, a Select for every "pick one"."""
    if isinstance(reply, ShortcutReply):
        return build_menu_message()  # handlers open the panel themselves; never reached normally
    if isinstance(reply, TimelineReply):
        return DiscordMessage(
            content=timeline_copy.timeline_text(reply.target_label, reply.entries)
        )
    if isinstance(reply, FallbackReply):
        menu = build_menu_message()
        hint = ai_copy.fallback_text(reply)
        if hint:
            menu.content = f"{hint}\n\n{menu.content}"
        return menu
    if isinstance(reply, NoticeReply):
        return DiscordMessage(content=ai_copy.notice_text(reply.notice))
    if isinstance(reply, DoneReply):
        return DiscordMessage(content=ai_copy.done_text(reply))
    if isinstance(reply, ConfirmReply):
        buttons = [ButtonSpec(label="ยืนยัน", custom_id="ai_confirm", style="primary")]
        if reply.can_skip_suggestions:
            buttons.append(
                ButtonSpec(label="ยืนยัน (ไม่ใส่ tag)", custom_id="ai_confirm_plain", style="secondary")
            )
        buttons.append(ButtonSpec(label="ยกเลิก", custom_id="cancel", style="danger"))
        return DiscordMessage(content=ai_copy.confirm_text(reply), buttons=buttons)
    if isinstance(reply, ChoiceReply):
        options = [
            SelectOptionSpec(label=_truncate(o.label, _OPTION_LABEL_MAX_LEN), value=o.value)
            for o in reply.options
        ]
        return DiscordMessage(
            content=ai_copy.choice_prompt(reply.kind, reply.subject),
            select=_select("ai_pick", "เลือก", options),
            buttons=[ButtonSpec(label="ยกเลิก", custom_id="cancel", style="danger")],
        )
    rows = reply.rows[:_SELECT_MAX_OPTIONS]
    select = (
        _select(
            "select_work_item",
            "เลือกงาน",
            [
                SelectOptionSpec(
                    label=_truncate(f"{row.label}: {row.name}", _OPTION_LABEL_MAX_LEN),
                    value=f"{row.project_id}:{row.work_item_id}",
                )
                for row in rows
            ],
        )
        if rows
        else None
    )
    return DiscordMessage(
        content=ai_copy.list_text(reply, max_rows=_SELECT_MAX_OPTIONS), select=select
    )


# --- Notifications (Phase 2B) -----------------------------------------------------------------


def build_preferences_panel(views: list[NotificationPreferenceView]) -> DiscordMessage:
    options = [
        SelectOptionSpec(
            label=_truncate(
                f"{notification_copy.toggle_label(view)} — "
                f"{notification_copy.EVENT_LABELS[view.event_type]}",
                _OPTION_LABEL_MAX_LEN,
            ),
            value=view.event_type.value,
        )
        for view in views
    ]
    return DiscordMessage(
        content=notification_copy.preferences_text(views),
        select=_select("notif_toggle", "เลือกเพื่อเปิด/ปิด", options),
    )


def build_summary_message(summary: DailySummary) -> DiscordMessage:
    return DiscordMessage(content=notification_copy.compose_daily_summary(summary))


# --- Groups (Phase 2D) ------------------------------------------------------------------------


def build_text(text: str) -> DiscordMessage:
    return DiscordMessage(content=text)


def build_group_workspace_picker(workspaces: list[WorkspaceConnection]) -> DiscordMessage:
    return DiscordMessage(
        content=group_copy.choose_workspace_text(),
        select=_select(
            "group_ws",
            "เลือก Workspace",
            [
                SelectOptionSpec(label=_truncate(ws.name, _OPTION_LABEL_MAX_LEN), value=str(ws.id))
                for ws in workspaces
            ],
        ),
    )


def build_group_project_picker(
    projects: list[ProjectRef], workspace_connection_id: str
) -> DiscordMessage:
    """A Select option carries one value, so it's "{workspace_connection_id}:{project_id|all}"
    (same joining trick as build_my_tasks_list; UUIDs never contain ":")."""
    options = [
        SelectOptionSpec(
            label=group_copy.ALL_PROJECTS_LABEL, value=f"{workspace_connection_id}:all"
        )
    ]
    options += [
        SelectOptionSpec(
            label=_truncate(p.name, _OPTION_LABEL_MAX_LEN),
            value=f"{workspace_connection_id}:{p.id}",
        )
        for p in projects
    ]
    return DiscordMessage(
        content=group_copy.choose_project_text(),
        select=_select("group_bind", "เลือก Project", options),
    )


def build_group_settings(binding: GroupBinding, *, just_bound: bool = False) -> DiscordMessage:
    text = group_copy.bound_text(binding) if just_bound else group_copy.settings_text(binding)
    return DiscordMessage(
        content=text,
        select=_select(
            "group_toggle",
            "เลือกเพื่อเปิด/ปิด",
            [
                SelectOptionSpec(
                    label=group_copy.toggle_label(binding, event_type), value=event_type.value
                )
                for event_type in GroupEventType
            ],
        ),
        buttons=[ButtonSpec(label="ยกเลิกผูกกลุ่ม", custom_id="group_unbind", style="danger")],
    )


def build_timeline_message(
    target_label: str | None, entries: list[TimelineEntry]
) -> DiscordMessage:
    """Phase 2E — same as LINE's: history plus the update menu to carry on."""
    menu = build_update_action_menu()
    menu.content = timeline_copy.timeline_text(target_label, entries) + "\n\nเลือกการอัปเดต:"
    return menu
