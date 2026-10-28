"""AiAssistant end to end over fakes: the spec's example sentences (AI MVP items 1, 3, 6, 7),
the confirm-before-write guarantee, and every "fall back to Phase 1" path."""

from __future__ import annotations

from datetime import date

import pytest

from src.application.ai.replies import (
    ChoiceReply,
    ConfirmReply,
    DoneReply,
    FallbackReply,
    ListReply,
    NoticeReply,
)
from src.domain.work_items.entities import WorkItemSearchResult
from src.shared.errors import AiUnavailableError, PromApiUnavailableError
from tests.ai.conftest import AI_PROJECT, CHANNEL_USER, PROM_USER, AiHarness


async def _state(h: AiHarness):  # type: ignore[no-untyped-def]
    return await h.conversation_states.get(h.installation_id, CHANNEL_USER)


def _search_hit(item_id: str, name: str, seq: int) -> WorkItemSearchResult:
    return WorkItemSearchResult(
        id=item_id, name=name, sequence_id=seq, project_id=AI_PROJECT.id, project_identifier="AI"
    )


# --- Off / failure paths ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_ai_falls_back_without_calling_llm(ai_harness: AiHarness) -> None:
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งานของฉันวันนี้")
    assert reply == FallbackReply(reason="disabled")
    assert ai_harness.llm_client.calls == []


@pytest.mark.asyncio
async def test_env_defaults_enable_ai_when_no_settings_saved(ai_harness: AiHarness) -> None:
    ai_harness.defaults = ai_harness.defaults.__class__(
        is_enabled=True,
        base_url=ai_harness.defaults.base_url,
        api_key="env-key",
        model="gpt-oss-120b",
        timeout_seconds=15,
    )
    ai_harness.says("hi", intent="show_menu")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "hi")
    assert reply == FallbackReply(reason="menu")
    assert ai_harness.llm_client.calls[0][0].api_key == "env-key"


@pytest.mark.asyncio
async def test_gateway_failure_falls_back_and_is_audited(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.llm_client.error = AiUnavailableError("down")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "อะไรก็ได้")
    assert reply == FallbackReply(reason="unavailable")
    entry = ai_harness.audit_log.entries[-1]
    assert entry.action == "ai.command_parse_failed"
    assert entry.error_code == "AI_UNAVAILABLE"


@pytest.mark.asyncio
async def test_schema_violation_falls_back(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says("x", intent="delete_everything")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "x")
    assert reply == FallbackReply(reason="unavailable")


@pytest.mark.asyncio
async def test_unknown_intent_is_not_understood(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "อากาศดีจัง")
    assert reply == FallbackReply(reason="not_understood")


@pytest.mark.asyncio
async def test_rate_limit(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    assistant = ai_harness.assistant
    for _ in range(30):
        await assistant.handle_text(ai_harness.session(), "hi")
    reply = await assistant.handle_text(ai_harness.session(), "hi")
    assert isinstance(reply, NoticeReply) and reply.notice.code == "rate_limited"


@pytest.mark.asyncio
async def test_prom_outage_is_a_notice_not_a_crash(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says("งาน overdue", intent="list_my_tasks", filter="overdue")
    ai_harness.prom_api_client.list_error = PromApiUnavailableError("down")
    ai_harness.seed_work_item("w1", "x", assignees=[PROM_USER])
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งาน overdue")
    assert isinstance(reply, NoticeReply)
    assert reply.notice.code == "error" and reply.notice.detail == "PROM_API_UNAVAILABLE"


@pytest.mark.asyncio
async def test_audit_never_stores_message_text(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says("ข้อความลับมาก", intent="show_menu")
    await ai_harness.assistant.handle_text(ai_harness.session(), "ข้อความลับมาก")
    entry = ai_harness.audit_log.entries[-1]
    assert entry.action == "ai.command_parsed" and entry.target_id == "show_menu"
    assert "ข้อความลับมาก" not in repr(entry)


# --- 1. AI task creation ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spec_example_create_needs_confirmation_then_creates(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    text = "สร้างงาน Deploy AI Platform ส่งวันศุกร์ Assign ให้ Somchai project AI"
    ai_harness.says(
        text,
        intent="create_work_item",
        title="Deploy AI Platform",
        project_ref="AI",
        assignee_ref="Somchai",
        due_expression="วันศุกร์",
        due_date="2026-10-31",  # wrong on purpose: the rule-based reading must win
    )
    assistant = ai_harness.assistant
    reply = await assistant.handle_text(ai_harness.session(), text)

    assert isinstance(reply, ConfirmReply)
    assert reply.action == "create"
    assert reply.title == "Deploy AI Platform"
    assert reply.project_name == "AI Platform"
    assert reply.assignee_name == "Somchai"
    assert reply.due_date == date(2026, 10, 30)
    assert reply.warnings == []
    assert ai_harness.prom_api_client.work_items == {}  # nothing written yet

    state = await _state(ai_harness)
    assert state is not None
    done = await assistant.confirm(ai_harness.session(), state)
    assert isinstance(done, DoneReply) and done.action == "create"
    assert done.target_label == "AI-1 Deploy AI Platform"
    assert done.web_url and done.web_url.startswith("https://prom.acme.example/acme/projects/p-ai")
    created = next(iter(ai_harness.prom_api_client.work_items.values()))
    assert created.assignee_ids == ["u-somchai"]
    assert created.target_date == date(2026, 10, 30)
    assert await _state(ai_harness) is None


@pytest.mark.asyncio
async def test_create_with_ambiguous_project_asks_then_continues(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says("สร้างงาน Fix login", intent="create_work_item", title="Fix login")
    assistant = ai_harness.assistant
    reply = await assistant.handle_text(ai_harness.session(), "สร้างงาน Fix login")
    assert isinstance(reply, ChoiceReply) and reply.kind == "project"
    assert [o.value for o in reply.options] == ["p-ai", "p-infra"]

    state = await _state(ai_harness)
    assert state is not None and state.step == "pick_project"
    confirm = await assistant.handle_choice(ai_harness.session(), state, "p-infra")
    assert isinstance(confirm, ConfirmReply) and confirm.project_name == "Infrastructure"


@pytest.mark.asyncio
async def test_create_warns_about_unknown_person_and_unreadable_date(
    ai_harness: AiHarness,
) -> None:
    await ai_harness.enable_ai()
    ai_harness.says(
        "t",
        intent="create_work_item",
        title="Write docs",
        project_ref="infra",
        assignee_ref="Ghost",
        due_expression="เร็วๆ นี้",
    )
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "t")
    assert isinstance(reply, ConfirmReply)
    assert reply.assignee_name is None and reply.due_date is None
    assert [w.code for w in reply.warnings] == ["member_not_found", "date_unreadable"]


@pytest.mark.asyncio
async def test_create_without_title_asks_for_one(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says("สร้างงาน", intent="create_work_item")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "สร้างงาน")
    assert isinstance(reply, NoticeReply) and reply.notice.code == "need_title"


@pytest.mark.asyncio
async def test_prompt_carries_today_and_project_names(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    await ai_harness.assistant.handle_text(ai_harness.session(), "hello")
    system_prompt = ai_harness.llm_client.calls[0][1]
    assert "2026-10-26 (Monday)" in system_prompt
    assert "AI Platform, Infrastructure" in system_prompt


# --- 3/7. Queries and search -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_tasks_today(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Due today", assignees=[PROM_USER], due=date(2026, 10, 26))
    ai_harness.seed_work_item("w2", "Later", assignees=[PROM_USER], due=date(2026, 11, 26), seq=2)
    ai_harness.seed_work_item("w3", "Not mine", assignees=["u-nok"], due=date(2026, 10, 26), seq=3)
    ai_harness.says("งานของฉันวันนี้", intent="list_my_tasks", filter="today")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งานของฉันวันนี้")
    assert isinstance(reply, ListReply) and reply.kind == "due_today"
    assert [r.work_item_id for r in reply.rows] == ["w1"]
    focus = await ai_harness.focus_store.get(ai_harness.installation_id, CHANNEL_USER)
    assert focus is not None and focus.work_item_id == "w1"


@pytest.mark.asyncio
async def test_project_tasks_without_personal_filter_is_a_project_search(
    ai_harness: AiHarness,
) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "A", seq=1)
    ai_harness.seed_work_item("w2", "B", seq=2)
    ai_harness.says("งานของ project AI", intent="list_my_tasks", project_ref="AI")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งานของ project AI")
    assert isinstance(reply, ListReply) and reply.kind == "project"
    assert reply.subject == "AI Platform"
    assert [r.work_item_id for r in reply.rows] == ["w2", "w1"]  # newest first


@pytest.mark.asyncio
async def test_search_by_keyword(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.prom_api_client.search_results = [_search_hit("w9", "Infra upgrade", 9)]
    ai_harness.says("งาน infra ล่าสุด", intent="search", keyword="infra")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งาน infra ล่าสุด")
    assert isinstance(reply, ListReply) and reply.kind == "search"
    assert [(r.label, r.name) for r in reply.rows] == [("AI-9", "Infra upgrade")]


@pytest.mark.asyncio
async def test_search_by_other_person(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Nok's task", assignees=["u-nok"])
    ai_harness.seed_work_item("w2", "Mine", assignees=[PROM_USER], seq=2)
    ai_harness.says("งานของ Nok", intent="search", assignee_ref="Nok")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งานของ Nok")
    assert isinstance(reply, ListReply) and reply.kind == "person" and reply.subject == "Nok"
    assert [r.work_item_id for r in reply.rows] == ["w1"]


@pytest.mark.asyncio
async def test_search_for_unknown_project(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says("งานของ project Mobile", intent="search", project_ref="Mobile")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "งานของ project Mobile")
    assert isinstance(reply, NoticeReply) and reply.notice.code == "project_not_found"


# --- 6. Status updates and other writes on an existing item ----------------------------------


@pytest.mark.asyncio
async def test_spec_example_done_by_name(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w12", "API Gateway", seq=12)
    ai_harness.prom_api_client.search_results = [_search_hit("w12", "API Gateway", 12)]
    text = "task API Gateway เสร็จแล้ว"
    ai_harness.says(text, intent="change_state", target_ref="API Gateway", state_ref="completed")
    assistant = ai_harness.assistant

    reply = await assistant.handle_text(ai_harness.session(), text)
    assert isinstance(reply, ConfirmReply) and reply.action == "complete"
    assert reply.target_label == "AI-12 API Gateway"
    assert ai_harness.prom_api_client.work_items["w12"].state_id == "s-todo"

    state = await _state(ai_harness)
    assert state is not None
    done = await assistant.confirm(ai_harness.session(), state)
    assert isinstance(done, DoneReply) and done.action == "complete"
    assert ai_harness.prom_api_client.work_items["w12"].state_id == "s-done"


@pytest.mark.asyncio
async def test_start_moves_to_started_group(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Login", seq=1)
    ai_harness.prom_api_client.search_results = [_search_hit("w1", "Login", 1)]
    ai_harness.says("เริ่มทำ Login", intent="change_state", target_ref="Login", state_ref="started")
    assistant = ai_harness.assistant
    reply = await assistant.handle_text(ai_harness.session(), "เริ่มทำ Login")
    assert isinstance(reply, ConfirmReply) and reply.state_name == "In Progress"
    state = await _state(ai_harness)
    assert state is not None
    await assistant.confirm(ai_harness.session(), state)
    assert ai_harness.prom_api_client.work_items["w1"].state_id == "s-doing"


@pytest.mark.asyncio
async def test_this_without_focus_asks_which_task(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.says(
        "mark complete", intent="change_state", target_ref="this", state_ref="completed"
    )
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "mark complete")
    assert isinstance(reply, NoticeReply) and reply.notice.code == "need_target"


@pytest.mark.asyncio
async def test_spec_example_assign_this_to_nok_uses_focus(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Login", seq=1, assignees=[PROM_USER])
    assistant = ai_harness.assistant
    await assistant.remember_focus(ai_harness.installation_id, CHANNEL_USER, AI_PROJECT.id, "w1")
    ai_harness.says("assign task นี้ให้ Nok", intent="reassign", target_ref="this", assignee_ref="Nok")
    reply = await assistant.handle_text(ai_harness.session(), "assign task นี้ให้ Nok")
    assert isinstance(reply, ConfirmReply) and reply.action == "reassign"
    assert reply.assignee_name == "Nok"
    assert reply.target_label == "AI-1 Login"  # label looked up because focus had none
    state = await _state(ai_harness)
    assert state is not None
    await assistant.confirm(ai_harness.session(), state)
    assert ai_harness.prom_api_client.work_items["w1"].assignee_ids == ["u-nok"]


@pytest.mark.asyncio
async def test_spec_example_reschedule_shifts_from_current_due_date(
    ai_harness: AiHarness,
) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Login", seq=1, due=date(2026, 10, 30))
    assistant = ai_harness.assistant
    await assistant.remember_focus(
        ai_harness.installation_id, CHANNEL_USER, AI_PROJECT.id, "w1", "AI-1 Login"
    )
    ai_harness.says(
        "เลื่อน deadline อีก 2 วัน",
        intent="reschedule",
        target_ref="this",
        due_expression="อีก 2 วัน",
        due_date="2026-10-28",  # the LLM resolved against today — must be ignored for a shift
    )
    reply = await assistant.handle_text(ai_harness.session(), "เลื่อน deadline อีก 2 วัน")
    assert isinstance(reply, ConfirmReply) and reply.action == "reschedule"
    assert reply.old_due_date == date(2026, 10, 30)
    assert reply.due_date == date(2026, 11, 1)
    state = await _state(ai_harness)
    assert state is not None
    await assistant.confirm(ai_harness.session(), state)
    assert ai_harness.prom_api_client.work_items["w1"].target_date == date(2026, 11, 1)


@pytest.mark.asyncio
async def test_ambiguous_target_asks_then_continues(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Login page", seq=1)
    ai_harness.seed_work_item("w2", "Login API", seq=2)
    ai_harness.prom_api_client.search_results = [
        _search_hit("w1", "Login page", 1),
        _search_hit("w2", "Login API", 2),
    ]
    ai_harness.says(
        "comment งาน Login ว่า รอ review",
        intent="add_comment",
        target_ref="Login",
        comment="รอ review",
    )
    assistant = ai_harness.assistant
    reply = await assistant.handle_text(ai_harness.session(), "comment งาน Login ว่า รอ review")
    assert isinstance(reply, ChoiceReply) and reply.kind == "work_item"
    assert len(reply.options) == 2

    state = await _state(ai_harness)
    assert state is not None
    confirm = await assistant.handle_choice(ai_harness.session(), state, reply.options[1].value)
    assert isinstance(confirm, ConfirmReply) and confirm.action == "comment"
    assert confirm.target_label == "AI-2 Login API" and confirm.comment == "รอ review"


@pytest.mark.asyncio
async def test_forged_choice_value_is_rejected(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.prom_api_client.search_results = [
        _search_hit("w1", "Login page", 1),
        _search_hit("w2", "Login API", 2),
    ]
    ai_harness.says("x", intent="change_state", target_ref="Login", state_ref="completed")
    assistant = ai_harness.assistant
    await assistant.handle_text(ai_harness.session(), "x")
    state = await _state(ai_harness)
    assert state is not None
    reply = await assistant.handle_choice(ai_harness.session(), state, "p-other:w-other")
    assert isinstance(reply, NoticeReply) and reply.notice.code == "session_expired"


@pytest.mark.asyncio
async def test_reassign_to_unknown_member(ai_harness: AiHarness) -> None:
    await ai_harness.enable_ai()
    ai_harness.seed_work_item("w1", "Login", seq=1)
    await ai_harness.assistant.remember_focus(
        ai_harness.installation_id, CHANNEL_USER, AI_PROJECT.id, "w1", "AI-1 Login"
    )
    ai_harness.says("ให้ Ghost", intent="reassign", target_ref="this", assignee_ref="Ghost")
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), "ให้ Ghost")
    assert isinstance(reply, NoticeReply) and reply.notice.code == "member_not_found"


@pytest.mark.asyncio
async def test_confirm_without_pending_state_does_nothing(ai_harness: AiHarness) -> None:
    from src.application.work_items.conversation import ConversationState

    reply = await ai_harness.assistant.confirm(
        ai_harness.session(), ConversationState(flow="ai", step="pick_project", data={})
    )
    assert isinstance(reply, NoticeReply) and reply.notice.code == "session_expired"
    assert ai_harness.prom_api_client.work_items == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "intent", "kind"),
    [
        ("สรุปงานให้หน่อย", "daily_summary", "daily_summary"),
        ("ปิดแจ้งเตือน overdue", "notification_settings", "notification_settings"),
    ],
)
async def test_summary_and_settings_intents_are_shortcuts(
    ai_harness: AiHarness, text: str, intent: str, kind: str
) -> None:
    from src.application.ai.replies import ShortcutReply

    await ai_harness.enable_ai()
    ai_harness.says(text, intent=intent)
    reply = await ai_harness.assistant.handle_text(ai_harness.session(), text)
    assert reply == ShortcutReply(kind=kind)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_history_intent_and_keyword(ai_harness: AiHarness) -> None:
    from datetime import datetime, timedelta

    from src.application.ai.replies import TimelineReply
    from src.application.work_items.timeline import GetWorkItemTimeline
    from src.domain.work_items.entities import WorkItemActivity

    h = ai_harness
    await h.enable_ai()
    h.seed_work_item("w12", "API Gateway", seq=12)
    h.prom_api_client.search_results = [_search_hit("w12", "API Gateway", 12)]
    h.prom_api_client.activities["w12"] = [
        WorkItemActivity(
            id="a",
            verb="created",
            field=None,
            old_value=None,
            new_value=None,
            actor_id=PROM_USER,
            created_at=datetime(2026, 10, 20, tzinfo=h.clock.now().tzinfo),
        )
    ]
    assistant = h.assistant
    assistant.get_timeline = h._uc(GetWorkItemTimeline)

    h.says("ประวัติงาน API Gateway", intent="history", target_ref="API Gateway")
    reply = await assistant.handle_text(h.session(), "ประวัติงาน API Gateway")
    assert isinstance(reply, TimelineReply)
    assert reply.target_label == "AI-12 API Gateway" and [e.kind for e in reply.entries] == [
        "created"
    ]

    # The keyword path uses the remembered work item and needs no LLM call.
    calls_before = len(h.llm_client.calls)
    again = await assistant.history_for_focus(h.session())
    assert isinstance(again, TimelineReply) and len(h.llm_client.calls) == calls_before
    assert timedelta  # noqa: B018 - imported for clarity of the fixture time maths above
