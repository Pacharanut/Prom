"""LineWebhookHandler — full conversation flows exercised through handle_payload(), fakes
underneath, real replies intercepted via the `sent_messages` fixture (see conftest.py). This is
the most valuable test file in this gate: it drives the actual controller a real LINE webhook
would hit, not just the channel-agnostic flow classes tested separately in
tests/application/test_conversation.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.channels.line.postback import decode_postback, encode_postback
from src.domain.identities.entities import DelegatedCredential, LinkingCode
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemState
from tests.channels.conftest import LineHandlerHarness

_USER = "Uabc123"


def _message_event(
    text: str, *, webhook_event_id: str = "wh-1", reply_token: str = "reply-1"
) -> dict:
    return {
        "events": [
            {
                "type": "message",
                "message": {"type": "text", "id": f"m-{webhook_event_id}", "text": text},
                "replyToken": reply_token,
                "webhookEventId": webhook_event_id,
                "timestamp": 1,
                "source": {"type": "user", "userId": _USER},
            }
        ]
    }


def _postback_event(
    data: str, *, webhook_event_id: str = "wh-1", reply_token: str = "reply-1"
) -> dict:
    return {
        "events": [
            {
                "type": "postback",
                "postback": {"data": data},
                "replyToken": reply_token,
                "webhookEventId": webhook_event_id,
                "timestamp": 1,
                "source": {"type": "user", "userId": _USER},
            }
        ]
    }


def _last_text(sent: list[tuple[str, str, list[dict]]]) -> str:
    return sent[-1][2][0]["text"]


def _last_postback_actions(sent: list[tuple[str, str, list[dict]]]) -> set[str]:
    message = sent[-1][2][0]
    items = message["quickReply"]["items"]
    return {decode_postback(item["action"]["data"])["action"] for item in items}


@pytest.mark.asyncio
async def test_duplicate_webhook_event_is_processed_once(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer = await line_harness.seed_customer()
    installation = await line_harness.seed_installation(customer.id)
    handler = line_harness.handler

    payload = _message_event("เมนู", webhook_event_id="dup-1")
    await handler.handle_payload(installation, "token-1", payload)
    await handler.handle_payload(installation, "token-1", payload)

    assert len(sent_messages) == 1


@pytest.mark.asyncio
async def test_otp_success_sends_confirmation(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer = await line_harness.seed_customer()
    instance = await line_harness.seed_prom_instance(customer.id)
    installation = await line_harness.seed_installation(customer.id)
    await line_harness.link_workspace(
        installation.id, (await line_harness.seed_workspace(customer.id, instance.id)).id
    )

    linking_code_id = uuid4()
    code = LinkingCode(
        id=linking_code_id,
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_installation_id=installation.id,
        code="123456",
        default_workspace_connection_id=None,
        expires_at=line_harness.clock.now().replace(year=2027),
        used_at=None,
        created_at=line_harness.clock.now(),
    )
    await line_harness.linking_codes.add(code)

    credential_token = line_harness.secrets_provider.encrypt(b"delegated-token")
    await line_harness.delegated_credentials.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_user_id="prom-user-1",
            encrypted_token=credential_token,
            created_at=line_harness.clock.now(),
            updated_at=line_harness.clock.now(),
            rotated_at=None,
        )
    )

    handler = line_harness.handler
    await handler.handle_payload(installation, "token-1", _message_event("123456"))

    assert len(sent_messages) == 1
    assert "เชื่อมต่อบัญชีสำเร็จ" in _last_text(sent_messages)
    linked = await line_harness.linked_identities.get_active_by_channel_user(installation.id, _USER)
    assert linked is not None
    assert linked.prom_user_id == "prom-user-1"


@pytest.mark.asyncio
async def test_otp_failure_sends_error_message(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer = await line_harness.seed_customer()
    installation = await line_harness.seed_installation(customer.id)
    handler = line_harness.handler

    await handler.handle_payload(installation, "token-1", _message_event("000000"))

    assert len(sent_messages) == 1
    assert "ไม่ถูกต้อง" in _last_text(sent_messages)


@pytest.mark.asyncio
async def test_unlinked_user_sending_non_otp_text_gets_not_linked_message(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer = await line_harness.seed_customer()
    installation = await line_harness.seed_installation(customer.id)
    handler = line_harness.handler

    await handler.handle_payload(installation, "token-1", _message_event("เมนู"))

    assert "ยังไม่ได้เชื่อมต่อ" in _last_text(sent_messages)


async def _linked_setup(line_harness: LineHandlerHarness):
    customer = await line_harness.seed_customer()
    instance = await line_harness.seed_prom_instance(customer.id)
    installation = await line_harness.seed_installation(customer.id)
    workspace = await line_harness.seed_workspace(customer.id, instance.id)
    await line_harness.link_workspace(installation.id, workspace.id)
    await line_harness.link_user(
        customer_id=customer.id,
        installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_user_id=_USER,
        default_workspace_connection_id=workspace.id,
    )
    return customer, instance, installation, workspace


@pytest.mark.asyncio
async def test_menu_command(line_harness: LineHandlerHarness, sent_messages: list) -> None:
    _, _, installation, _ = await _linked_setup(line_harness)
    handler = line_harness.handler
    await handler.handle_payload(installation, "token-1", _message_event("เมนู"))
    actions = _last_postback_actions(sent_messages)
    assert actions == {"create", "my_tasks", "update"}


@pytest.mark.asyncio
async def test_tapping_menu_quick_reply_buttons_routes_same_as_typed_keywords(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    """Regression test: `build_menu_message()`'s Quick Reply buttons encode `action` as
    "create"/"my_tasks"/"update" postbacks (see `_last_postback_actions` above), but
    `_handle_postback` used to only handle "cancel"/"select_workspace"/"select_work_item" plus
    active-flow-scoped actions — tapping a menu button (no active flow yet) fell through to
    "state is None: return" and silently did nothing, while typing the identical Thai keyword
    worked fine. Fixed in `LineWebhookHandler._handle_postback` to route these three actions
    through the same `_start_or_defer` helper the text path already uses.
    """
    _, instance, installation, _ = await _linked_setup(line_harness)
    handler = line_harness.handler

    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("create"), webhook_event_id="btn-1"),
    )
    assert "ตั้งชื่องาน" in _last_text(sent_messages)

    # "my_tasks" tap: same underlying `_show_my_tasks` path "งานของฉัน" text uses (verified against
    # `test_my_tasks_shows_assigned_items`'s identical setup/assertion shape) — must render the
    # seeded item, not silently do nothing.
    line_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    await line_harness.prom_api_client.create_work_item(
        instance.base_url,
        "delegated-token",
        "acme",
        "p1",
        name="My task",
        state_id=None,
        assignee_ids=["prom-user-1"],
        target_date=None,
        description_html=None,
        external_id="seed",
        external_source="test",
    )
    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("my_tasks"), webhook_event_id="btn-2"),
    )
    assert "ENG-" in _last_text(sent_messages)


@pytest.mark.asyncio
async def test_full_create_flow_end_to_end(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    _, instance, installation, _ = await _linked_setup(line_harness)
    line_harness.prom_api_client.projects = [
        ProjectRef(id="p1", name="Engineering", identifier="ENG")
    ]
    line_harness.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="s1", name="Todo", group="unstarted")
    ]
    line_harness.prom_api_client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="m1", display_name="Alice")
    ]
    handler = line_harness.handler

    await handler.handle_payload(
        installation, "token-1", _message_event("สร้างงาน", webhook_event_id="w1")
    )
    assert "ตั้งชื่องาน" in _last_text(sent_messages)

    await handler.handle_payload(
        installation, "token-1", _message_event("Fix the bug", webhook_event_id="w2")
    )
    assert _last_postback_actions(sent_messages) == {"select_project"}

    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("select_project", value="p1"), webhook_event_id="w3"),
    )
    assert _last_postback_actions(sent_messages) == {"select_state"}

    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("select_state", value="s1"), webhook_event_id="w4"),
    )
    assert _last_postback_actions(sent_messages) == {"select_assignee"}

    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("select_assignee", value="m1"), webhook_event_id="w5"),
    )
    assert "กำหนดส่ง" in _last_text(sent_messages)

    await handler.handle_payload(
        installation, "token-1", _message_event("2026-03-01", webhook_event_id="w6")
    )
    assert "ตรวจสอบข้อมูล" in _last_text(sent_messages)
    assert _last_postback_actions(sent_messages) == {"confirm", "cancel"}

    await handler.handle_payload(
        installation, "token-1", _postback_event(encode_postback("confirm"), webhook_event_id="w7")
    )
    assert "สร้างงานสำเร็จ" in _last_text(sent_messages)
    assert "ENG-" in _last_text(sent_messages)

    # Conversation state must be cleared after completion.
    assert await line_harness.conversation_states.get(installation.id, _USER) is None


@pytest.mark.asyncio
async def test_cancel_mid_flow_clears_state(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    _, _, installation, _ = await _linked_setup(line_harness)
    line_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    handler = line_harness.handler

    await handler.handle_payload(
        installation, "token-1", _message_event("สร้างงาน", webhook_event_id="c1")
    )
    await handler.handle_payload(
        installation, "token-1", _message_event("ยกเลิก", webhook_event_id="c2")
    )

    assert "ยกเลิกแล้ว" in _last_text(sent_messages)
    assert await line_harness.conversation_states.get(installation.id, _USER) is None


@pytest.mark.asyncio
async def test_invalid_due_date_reprompts_without_advancing(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    _, _, installation, _ = await _linked_setup(line_harness)
    line_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    line_harness.prom_api_client.states_by_project["p1"] = []
    line_harness.prom_api_client.members_by_project["p1"] = []
    handler = line_harness.handler

    await handler.handle_payload(
        installation, "token-1", _message_event("สร้างงาน", webhook_event_id="d1")
    )
    await handler.handle_payload(
        installation, "token-1", _message_event("Task", webhook_event_id="d2")
    )
    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("select_project", value="p1"), webhook_event_id="d3"),
    )
    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("select_state", value="skip"), webhook_event_id="d4"),
    )
    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(encode_postback("select_assignee", value="skip"), webhook_event_id="d5"),
    )
    await handler.handle_payload(
        installation, "token-1", _message_event("not-a-date", webhook_event_id="d6")
    )
    assert "กำหนดส่ง" in _last_text(sent_messages)

    state = await line_harness.conversation_states.get(installation.id, _USER)
    assert state is not None and state.step == "due_date"


@pytest.mark.asyncio
async def test_my_tasks_shows_assigned_items(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    _, instance, installation, _ = await _linked_setup(line_harness)
    line_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    work_item, _ = await line_harness.prom_api_client.create_work_item(
        instance.base_url,
        "delegated-token",
        "acme",
        "p1",
        name="My task",
        state_id=None,
        assignee_ids=["prom-user-1"],
        target_date=None,
        description_html=None,
        external_id="seed",
        external_source="test",
    )
    handler = line_harness.handler

    await handler.handle_payload(installation, "token-1", _message_event("งานของฉัน"))
    assert "ENG-" in _last_text(sent_messages)
    assert work_item.name in _last_text(sent_messages)


@pytest.mark.asyncio
async def test_tapping_my_tasks_item_starts_update_flow_and_mark_complete(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    _, instance, installation, _ = await _linked_setup(line_harness)
    line_harness.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="done", name="Done", group="completed")
    ]
    work_item, _ = await line_harness.prom_api_client.create_work_item(
        instance.base_url,
        "delegated-token",
        "acme",
        "p1",
        name="Task to complete",
        state_id=None,
        assignee_ids=["prom-user-1"],
        target_date=None,
        description_html=None,
        external_id="seed2",
        external_source="test",
    )
    handler = line_harness.handler

    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(
            encode_postback("select_work_item", value=work_item.id, project="p1"),
            webhook_event_id="u1",
        ),
    )
    assert _last_postback_actions(sent_messages) == {"select_update_action", "cancel"}

    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(
            encode_postback("select_update_action", value="complete"), webhook_event_id="u2"
        ),
    )
    assert _last_postback_actions(sent_messages) == {"confirm", "cancel"}

    await handler.handle_payload(
        installation, "token-1", _postback_event(encode_postback("confirm"), webhook_event_id="u3")
    )
    assert "สำเร็จ" in _last_text(sent_messages)
    assert work_item.state_id == "done"
    assert await line_harness.conversation_states.get(installation.id, _USER) is None


@pytest.mark.asyncio
async def test_workspace_selection_when_multiple_workspaces_and_no_default(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    customer = await line_harness.seed_customer()
    instance = await line_harness.seed_prom_instance(customer.id)
    installation = await line_harness.seed_installation(customer.id)
    ws_a = await line_harness.seed_workspace(customer.id, instance.id)
    ws_b = await line_harness.seed_workspace(customer.id, instance.id)
    await line_harness.link_workspace(installation.id, ws_a.id)
    await line_harness.link_workspace(installation.id, ws_b.id)
    await line_harness.link_user(
        customer_id=customer.id,
        installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_user_id=_USER,
        default_workspace_connection_id=None,
    )
    line_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    handler = line_harness.handler

    await handler.handle_payload(
        installation, "token-1", _message_event("งานของฉัน", webhook_event_id="ws1")
    )
    fields_list = [
        decode_postback(item["action"]["data"])
        for item in sent_messages[-1][2][0]["quickReply"]["items"]
    ]
    assert {f["action"] for f in fields_list} == {"select_workspace"}

    chosen_workspace_id = fields_list[0]["value"]
    await handler.handle_payload(
        installation,
        "token-1",
        _postback_event(
            encode_postback("select_workspace", value=chosen_workspace_id), webhook_event_id="ws2"
        ),
    )
    # Resumes the originally-requested action (my_tasks) once a workspace is chosen.
    assert "ไม่พบงานที่ได้รับมอบหมาย" in _last_text(sent_messages) or "ENG-" in _last_text(sent_messages)
