"""The AI assistant driven through the real LINE and Discord controllers: free text goes to the
assistant only when AI is on, keywords still win, and confirm/pick buttons round-trip."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from src.application.ai.assistant import AiAssistant
from src.application.ai.intent_parser import IntentParser
from src.application.ai.ports import AiDefaults
from src.application.ai.settings import ResolveAiConnection
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    GetWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListProjectWorkItems,
    ListStates,
    MarkComplete,
    RescheduleWorkItem,
    SearchWorkItems,
    UpdateWorkItemState,
)
from src.channels.discord.events import DiscordEvent
from src.channels.line.postback import decode_postback, encode_postback
from src.domain.ai.entities import AiSettings
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemState
from tests.channels.conftest import DiscordHandlerHarness, LineHandlerHarness
from tests.fakes.repositories import FakeAiSettingsRepository
from tests.fakes.support import FakeFocusStore, FakeLlmClient

_LINE_USER = "Uai"
_DISCORD_USER = "d-ai"
_PROJECT = ProjectRef(id="p-ai", name="AI Platform", identifier="AI")


def _build_assistant(
    harness: LineHandlerHarness | DiscordHandlerHarness,
    llm: FakeLlmClient,
    ai_settings: FakeAiSettingsRepository,
) -> AiAssistant:
    def uc(cls: Any) -> Any:
        return cls(
            prom_instances=harness.prom_instances,
            delegated_credentials=harness.delegated_credentials,
            secrets_provider=harness.secrets_provider,
            prom_api_client=harness.prom_api_client,
            audit_log=harness.audit_log,
            clock=harness.clock,
        )

    return AiAssistant(
        resolve_connection=ResolveAiConnection(
            ai_settings=ai_settings,
            secrets_provider=harness.secrets_provider,
            defaults=AiDefaults(
                is_enabled=False, base_url=None, api_key=None, model="m", timeout_seconds=15
            ),
        ),
        intent_parser=IntentParser(llm_client=llm),
        conversation_states=harness.conversation_states,
        focus_store=FakeFocusStore(),
        rate_limiter=harness.rate_limiter,
        prom_instances=harness.prom_instances,
        audit_log=harness.audit_log,
        clock=harness.clock,
        list_projects=uc(ListProjects),
        list_states=uc(ListStates),
        list_project_members=uc(ListProjectMembers),
        list_my_work_items=uc(ListMyWorkItems),
        list_project_work_items=uc(ListProjectWorkItems),
        search_work_items=uc(SearchWorkItems),
        get_work_item=uc(GetWorkItem),
        create_work_item=uc(CreateWorkItem),
        update_work_item_state=uc(UpdateWorkItemState),
        mark_complete=uc(MarkComplete),
        change_assignee=uc(ChangeAssignee),
        reschedule_work_item=uc(RescheduleWorkItem),
        add_comment=uc(AddComment),
    )


async def _enable(
    harness: LineHandlerHarness | DiscordHandlerHarness,
    ai_settings: FakeAiSettingsRepository,
    customer_id: Any,
) -> None:
    now = harness.clock.now()
    await ai_settings.save(
        AiSettings(
            id=uuid4(),
            customer_id=customer_id,
            is_enabled=True,
            base_url="https://aihub.example/core-api/v1",
            encrypted_api_key=harness.secrets_provider.encrypt(b"k"),
            model="gpt-oss-120b",
            timeout_seconds=15,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_plane(harness: LineHandlerHarness | DiscordHandlerHarness) -> None:
    harness.prom_api_client.projects = [_PROJECT]
    harness.prom_api_client.states_by_project = {
        _PROJECT.id: [WorkItemState(id="s-todo", name="Todo", group="unstarted")]
    }
    harness.prom_api_client.members_by_project = {
        _PROJECT.id: [ProjectMemberRef(user_id="u-somchai", display_name="Somchai")]
    }


_CREATE_TEXT = "สร้างงาน Deploy AI Platform ส่งพรุ่งนี้ ให้ Somchai"
_CREATE_COMMAND = {
    "intent": "create_work_item",
    "title": "Deploy AI Platform",
    "assignee_ref": "Somchai",
    "due_expression": "พรุ่งนี้",
}


def _line_message(text: str, event_id: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "message",
                "message": {"type": "text", "id": f"m-{event_id}", "text": text},
                "replyToken": f"r-{event_id}",
                "webhookEventId": event_id,
                "timestamp": 1,
                "source": {"type": "user", "userId": _LINE_USER},
            }
        ]
    }


def _line_postback(data: str, event_id: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "postback",
                "postback": {"data": data},
                "replyToken": f"r-{event_id}",
                "webhookEventId": event_id,
                "timestamp": 1,
                "source": {"type": "user", "userId": _LINE_USER},
            }
        ]
    }


async def _line_setup(h: LineHandlerHarness) -> Any:
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    workspace = await h.seed_workspace(customer.id, instance.id)
    installation = await h.seed_installation(customer.id)
    await h.link_workspace(installation.id, workspace.id)
    await h.link_user(
        customer_id=customer.id,
        installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id="u-me",
        channel_user_id=_LINE_USER,
        default_workspace_connection_id=workspace.id,
    )
    _seed_plane(h)
    return customer, installation


@pytest.mark.asyncio
async def test_line_free_text_without_ai_shows_menu(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer, installation = await _line_setup(line_harness)
    llm = FakeLlmClient(replies={_CREATE_TEXT: _CREATE_COMMAND})
    ai_settings = FakeAiSettingsRepository()  # nothing saved, env defaults off
    handler = replace(
        line_harness.handler, assistant=_build_assistant(line_harness, llm, ai_settings)
    )

    await handler.handle_payload(installation, "token-1", _line_message(_CREATE_TEXT, "e1"))

    assert "เลือกเมนูที่ต้องการ" in sent_messages[-1][2][0]["text"]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_line_keywords_still_win_over_ai(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer, installation = await _line_setup(line_harness)
    llm = FakeLlmClient()
    ai_settings = FakeAiSettingsRepository()
    await _enable(line_harness, ai_settings, customer.id)
    handler = replace(
        line_harness.handler, assistant=_build_assistant(line_harness, llm, ai_settings)
    )

    await handler.handle_payload(installation, "token-1", _line_message("สร้างงาน", "e1"))

    assert sent_messages[-1][2][0]["text"].startswith("ตั้งชื่องาน")
    assert llm.calls == []


@pytest.mark.asyncio
async def test_line_ai_create_confirm_round_trip(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer, installation = await _line_setup(line_harness)
    llm = FakeLlmClient(replies={_CREATE_TEXT: _CREATE_COMMAND})
    ai_settings = FakeAiSettingsRepository()
    await _enable(line_harness, ai_settings, customer.id)
    handler = replace(
        line_harness.handler, assistant=_build_assistant(line_harness, llm, ai_settings)
    )

    await handler.handle_payload(installation, "token-1", _line_message(_CREATE_TEXT, "e1"))
    confirm = sent_messages[-1][2][0]
    assert "ตรวจสอบก่อนสร้างงาน" in confirm["text"]
    assert "Somchai" in confirm["text"] and "2 ม.ค. 2026" in confirm["text"]
    actions = [
        decode_postback(i["action"]["data"])["action"] for i in confirm["quickReply"]["items"]
    ]
    assert actions == ["ai_confirm", "cancel"]
    assert line_harness.prom_api_client.work_items == {}

    await handler.handle_payload(
        installation, "token-1", _line_postback(encode_postback("ai_confirm"), "e2")
    )
    assert "สร้างงานสำเร็จ" in sent_messages[-1][2][0]["text"]
    created = next(iter(line_harness.prom_api_client.work_items.values()))
    assert created.name == "Deploy AI Platform"
    assert created.assignee_ids == ["u-somchai"]


@pytest.mark.asyncio
async def test_line_stale_ai_confirm_is_handled(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer, installation = await _line_setup(line_harness)
    ai_settings = FakeAiSettingsRepository()
    await _enable(line_harness, ai_settings, customer.id)
    handler = replace(
        line_harness.handler,
        assistant=_build_assistant(line_harness, FakeLlmClient(), ai_settings),
    )

    await handler.handle_payload(
        installation, "token-1", _line_postback(encode_postback("ai_confirm"), "e1")
    )

    assert "หมดอายุ" in sent_messages[-1][2][0]["text"]
    assert line_harness.prom_api_client.work_items == {}


@pytest.mark.asyncio
async def test_discord_ai_create_confirm_round_trip(discord_harness: DiscordHandlerHarness) -> None:
    h = discord_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    workspace = await h.seed_workspace(customer.id, instance.id)
    installation = await h.seed_installation(customer.id)
    await h.link_workspace(installation.id, workspace.id)
    await h.link_user(
        customer_id=customer.id,
        installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id="u-me",
        channel_user_id=_DISCORD_USER,
        default_workspace_connection_id=workspace.id,
    )
    _seed_plane(h)
    ai_settings = FakeAiSettingsRepository()
    await _enable(h, ai_settings, customer.id)
    llm = FakeLlmClient(replies={_CREATE_TEXT: _CREATE_COMMAND})
    handler = replace(h.handler, assistant=_build_assistant(h, llm, ai_settings))
    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    await handler.handle_event(
        installation,
        DiscordEvent(
            kind="message",
            user_id=_DISCORD_USER,
            text=_CREATE_TEXT,
            component_custom_id=None,
            component_value=None,
            dedup_id="m1",
        ),
        send,
    )
    assert "ตรวจสอบก่อนสร้างงาน" in sent[-1].content
    assert [b.custom_id for b in sent[-1].buttons] == ["ai_confirm", "cancel"]

    await handler.handle_event(
        installation,
        DiscordEvent(
            kind="component",
            user_id=_DISCORD_USER,
            text=None,
            component_custom_id="ai_confirm",
            component_value=None,
            dedup_id="i1",
        ),
        send,
    )
    assert "สร้างงานสำเร็จ" in sent[-1].content
    assert len(h.prom_api_client.work_items) == 1
