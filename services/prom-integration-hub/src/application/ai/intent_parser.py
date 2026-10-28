"""IntentParser — turns one chat message into a validated ParsedCommand via the LlmClient port.
Owns the system prompt (application knowledge: which intents exist and what each field means);
the adapter only owns HTTP. The user's message is passed as the *user* turn, never spliced into
the system prompt, and whatever comes back is validated against ParsedCommand before anything
else sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from src.application.ai.commands import COMMAND_JSON_SCHEMA, ParsedCommand
from src.application.ai.ports import AiConnection, LlmClient
from src.shared.errors import AiResponseInvalidError

_MAX_MESSAGE_CHARS = 500
_MAX_PROJECT_NAMES = 10
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_SYSTEM_PROMPT = """You convert one chat message (Thai or English) from a user of a project-management tool into a JSON command. You only describe what the user asked; you never perform actions.

Today is {today} ({weekday}), timezone Asia/Bangkok.
The user's projects: {projects}

intent — exactly one of:
- create_work_item: make a new task. ("สร้างงาน ...", "create a task ...", "เพิ่มงาน ...")
- list_my_tasks: the user's OWN tasks, optionally filtered by date ("งานของฉันวันนี้", "งาน overdue", "my tasks", "งานที่ assign ให้ฉัน").
- search: find tasks by keyword, by project, or by another person ("งาน infra ล่าสุด", "งานของ project AI", "งานของ Nok", "หางาน login").
- change_state: start or finish an existing task ("task API Gateway เสร็จแล้ว", "เริ่มทำงาน X", "mark complete", "ปิดงานนี้").
- reassign: give an existing task to someone ("assign task นี้ให้ Nok", "โอนงาน X ให้ Somchai").
- reschedule: change an existing task's due date ("เลื่อน deadline อีก 2 วัน", "เปลี่ยนกำหนดส่งงาน X เป็นวันศุกร์").
- add_comment: comment on an existing task ("comment งาน X ว่า รอ review").
- daily_summary: wants an overview/summary of their work right now ("สรุปงานให้หน่อย", "สรุปงานวันนี้", "daily summary").
- history: wants the change history of an existing task ("ประวัติงาน X", "ใครแก้งานนี้", "history of PROM-12").
- notification_settings: wants to see or change which notifications they get ("ตั้งค่าแจ้งเตือน", "ปิดแจ้งเตือน overdue", "ไม่อยากได้แจ้งเตือน").
- show_menu: greetings, help, or asking what the bot can do.
- unknown: anything else.

Fields (use null when not mentioned):
- title: the new task's name, without command words or the date/person/project parts.
- target_ref: the existing task the user means, in their words (a name or an id like PROM-12). Use "this" when they say นี้/this/it or don't name one.
- project_ref: project name as written (drop the word "project"/"โปรเจกต์").
- assignee_ref: person as written; use "me" for ฉัน/ผม/me/myself.
- due_expression: the date phrase copied exactly as written ("วันศุกร์", "สิ้นเดือน", "พรุ่งนี้"). For a shift like "เลื่อนอีก 2 วัน" write "อีก 2 วัน".
- due_date: due_expression resolved to YYYY-MM-DD using today's date, or null.
- state_ref: "started" for start/in progress/กำลังทำ/เริ่ม, "completed" for done/finished/เสร็จ/ปิด; otherwise null.
- filter (list_my_tasks only): "today" (วันนี้/due today), "overdue" (เลยกำหนด/overdue), "due_soon" (ใกล้ครบกำหนด/พรุ่งนี้), "assigned" (all mine). Otherwise null.
- keyword (search only): words to search task names for — NOT a project or person. "งานของ project AI" → project_ref "AI", keyword null. "งานของ Nok" → assignee_ref "Nok", keyword null. "งาน infra ล่าสุด" → keyword "infra".
- comment (add_comment only): the comment text.
- priority (create_work_item only): "urgent" for ด่วนมาก/ด่วนที่สุด/urgent/ASAP, "high" for ด่วน/สำคัญ/high priority, "medium", "low" for ไม่ด่วน/low priority — ONLY if the user says so; otherwise null.

Reply with the JSON object only."""


def build_system_prompt(today: date, project_names: list[str]) -> str:
    names = ", ".join(project_names[:_MAX_PROJECT_NAMES]) or "(unknown)"
    return _SYSTEM_PROMPT.format(
        today=today.isoformat(), weekday=_WEEKDAY_NAMES[today.weekday()], projects=names
    )


@dataclass
class IntentParser:
    llm_client: LlmClient

    async def parse(
        self, connection: AiConnection, text: str, *, today: date, project_names: list[str]
    ) -> ParsedCommand:
        raw = await self.llm_client.complete_json(
            connection,
            system_prompt=build_system_prompt(today, project_names),
            user_message=text[:_MAX_MESSAGE_CHARS],
            schema_name="prom_command",
            schema=COMMAND_JSON_SCHEMA,
        )
        try:
            return ParsedCommand.model_validate(raw)
        except ValidationError as exc:
            raise AiResponseInvalidError(
                f"LLM reply did not match the command schema ({exc.error_count()} errors)"
            ) from exc
