"""DiscordWebhookHandler — full conversation flows exercised through handle_event(), fakes
underneath. Discord counterpart to tests/channels/test_line_handler.py; every scenario there has a
match here, minus LINE-specific mechanics (reply tokens, postback query-string data) that don't
exist on this transport — see DiscordEvent/DiscordMessage instead.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.channels.discord.components import DiscordMessage
from src.channels.discord.events import DiscordEvent
from src.domain.identities.entities import DelegatedCredential, LinkingCode
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemState
from tests.channels.conftest import DiscordHandlerHarness

_USER = "111111111111111111"  # a Discord snowflake, as a string


def _message_event(text: str, *, dedup_id: str = "m-1") -> DiscordEvent:
    return DiscordEvent(
        kind="message",
        user_id=_USER,
        text=text,
        component_custom_id=None,
        component_value=None,
        dedup_id=dedup_id,
    )


def _component_event(
    custom_id: str, value: str | None = None, *, dedup_id: str = "i-1"
) -> DiscordEvent:
    return DiscordEvent(
        kind="component",
        user_id=_USER,
        text=None,
        component_custom_id=custom_id,
        component_value=value,
        dedup_id=dedup_id,
    )


def _last(sent: list[DiscordMessage]) -> DiscordMessage:
    return sent[-1]


def _component_ids(message: DiscordMessage) -> set[str]:
    ids = {b.custom_id for b in message.buttons}
    if message.select is not None:
        ids.add(message.select.custom_id)
    return ids


@pytest.fixture
def sent() -> list[DiscordMessage]:
    return []


async def _send_into(collector: list[DiscordMessage], message: DiscordMessage) -> None:
    collector.append(message)


@pytest.mark.asyncio
async def test_duplicate_event_is_processed_once(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    customer = await discord_harness.seed_customer()
    installation = await discord_harness.seed_installation(customer.id)
    handler = discord_harness.handler
    event = _message_event("เมนู", dedup_id="dup-1")

    await handler.handle_event(installation, event, lambda m: _send_into(sent, m))
    await handler.handle_event(installation, event, lambda m: _send_into(sent, m))

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_otp_success_sends_confirmation(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    customer = await discord_harness.seed_customer()
    instance = await discord_harness.seed_prom_instance(customer.id)
    installation = await discord_harness.seed_installation(customer.id)

    code = LinkingCode(
        id=uuid4(),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_installation_id=installation.id,
        code="123456",
        default_workspace_connection_id=None,
        expires_at=discord_harness.clock.now().replace(year=2027),
        used_at=None,
        created_at=discord_harness.clock.now(),
    )
    await discord_harness.linking_codes.add(code)

    credential_token = discord_harness.secrets_provider.encrypt(b"delegated-token")
    await discord_harness.delegated_credentials.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_user_id="prom-user-1",
            encrypted_token=credential_token,
            created_at=discord_harness.clock.now(),
            updated_at=discord_harness.clock.now(),
            rotated_at=None,
        )
    )

    handler = discord_harness.handler
    await handler.handle_event(
        installation, _message_event("123456"), lambda m: _send_into(sent, m)
    )

    assert len(sent) == 1
    assert "เชื่อมต่อบัญชีสำเร็จ" in _last(sent).content
    linked = await discord_harness.linked_identities.get_active_by_channel_user(
        installation.id, _USER
    )
    assert linked is not None
    assert linked.prom_user_id == "prom-user-1"


@pytest.mark.asyncio
async def test_otp_failure_sends_error_message(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    customer = await discord_harness.seed_customer()
    installation = await discord_harness.seed_installation(customer.id)
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _message_event("000000"), lambda m: _send_into(sent, m)
    )

    assert len(sent) == 1
    assert "ไม่ถูกต้อง" in _last(sent).content


@pytest.mark.asyncio
async def test_unlinked_user_sending_non_otp_text_gets_not_linked_message(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    customer = await discord_harness.seed_customer()
    installation = await discord_harness.seed_installation(customer.id)
    handler = discord_harness.handler

    await handler.handle_event(installation, _message_event("เมนู"), lambda m: _send_into(sent, m))

    assert "ยังไม่ได้เชื่อมต่อ" in _last(sent).content


async def _linked_setup(discord_harness: DiscordHandlerHarness):
    customer = await discord_harness.seed_customer()
    instance = await discord_harness.seed_prom_instance(customer.id)
    installation = await discord_harness.seed_installation(customer.id)
    workspace = await discord_harness.seed_workspace(customer.id, instance.id)
    await discord_harness.link_workspace(installation.id, workspace.id)
    await discord_harness.link_user(
        customer_id=customer.id,
        installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_user_id=_USER,
        default_workspace_connection_id=workspace.id,
    )
    return customer, instance, installation, workspace


@pytest.mark.asyncio
async def test_menu_command(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    _, _, installation, _ = await _linked_setup(discord_harness)
    handler = discord_harness.handler
    await handler.handle_event(installation, _message_event("เมนู"), lambda m: _send_into(sent, m))
    assert _component_ids(_last(sent)) == {
        "menu_create",
        "menu_my_tasks",
        "menu_update",
        "menu_summary",
        "menu_notif_settings",
    }


@pytest.mark.asyncio
async def test_tapping_menu_button_starts_create_flow(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    """Unlike Gate 5's LINE menu (whose Quick Reply buttons are decorative — only the typed
    keyword actually triggers a flow, see the parent conversation's Gate 6 finding), tapping a
    Discord menu button must work, since components are Discord's primary interaction surface."""
    _, _, installation, _ = await _linked_setup(discord_harness)
    handler = discord_harness.handler
    await handler.handle_event(
        installation, _component_event("menu_create"), lambda m: _send_into(sent, m)
    )
    assert "ตั้งชื่องาน" in _last(sent).content


@pytest.mark.asyncio
async def test_full_create_flow_end_to_end(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    _, instance, installation, _ = await _linked_setup(discord_harness)
    discord_harness.prom_api_client.projects = [
        ProjectRef(id="p1", name="Engineering", identifier="ENG")
    ]
    discord_harness.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="s1", name="Todo", group="unstarted")
    ]
    discord_harness.prom_api_client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="m1", display_name="Alice")
    ]
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _message_event("สร้างงาน", dedup_id="w1"), lambda m: _send_into(sent, m)
    )
    assert "ตั้งชื่องาน" in _last(sent).content

    await handler.handle_event(
        installation, _message_event("Fix the bug", dedup_id="w2"), lambda m: _send_into(sent, m)
    )
    assert _component_ids(_last(sent)) == {"select_project"}

    await handler.handle_event(
        installation,
        _component_event("select_project", "p1", dedup_id="w3"),
        lambda m: _send_into(sent, m),
    )
    assert _component_ids(_last(sent)) == {"select_state"}

    await handler.handle_event(
        installation,
        _component_event("select_state", "s1", dedup_id="w4"),
        lambda m: _send_into(sent, m),
    )
    assert _component_ids(_last(sent)) == {"select_assignee"}

    await handler.handle_event(
        installation,
        _component_event("select_assignee", "m1", dedup_id="w5"),
        lambda m: _send_into(sent, m),
    )
    assert "กำหนดส่ง" in _last(sent).content

    await handler.handle_event(
        installation, _message_event("2026-03-01", dedup_id="w6"), lambda m: _send_into(sent, m)
    )
    assert "ตรวจสอบข้อมูล" in _last(sent).content
    assert _component_ids(_last(sent)) == {"confirm", "cancel"}

    await handler.handle_event(
        installation, _component_event("confirm", dedup_id="w7"), lambda m: _send_into(sent, m)
    )
    assert "สร้างงานสำเร็จ" in _last(sent).content
    assert "ENG-" in _last(sent).content

    assert await discord_harness.conversation_states.get(installation.id, _USER) is None


@pytest.mark.asyncio
async def test_cancel_mid_flow_clears_state(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    _, _, installation, _ = await _linked_setup(discord_harness)
    discord_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _message_event("สร้างงาน", dedup_id="c1"), lambda m: _send_into(sent, m)
    )
    await handler.handle_event(
        installation, _message_event("ยกเลิก", dedup_id="c2"), lambda m: _send_into(sent, m)
    )

    assert "ยกเลิกแล้ว" in _last(sent).content
    assert await discord_harness.conversation_states.get(installation.id, _USER) is None


@pytest.mark.asyncio
async def test_invalid_due_date_reprompts_without_advancing(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    _, _, installation, _ = await _linked_setup(discord_harness)
    discord_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    discord_harness.prom_api_client.states_by_project["p1"] = []
    discord_harness.prom_api_client.members_by_project["p1"] = []
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _message_event("สร้างงาน", dedup_id="d1"), lambda m: _send_into(sent, m)
    )
    await handler.handle_event(
        installation, _message_event("Task", dedup_id="d2"), lambda m: _send_into(sent, m)
    )
    await handler.handle_event(
        installation,
        _component_event("select_project", "p1", dedup_id="d3"),
        lambda m: _send_into(sent, m),
    )
    await handler.handle_event(
        installation,
        _component_event("select_state", "skip", dedup_id="d4"),
        lambda m: _send_into(sent, m),
    )
    await handler.handle_event(
        installation,
        _component_event("select_assignee", "skip", dedup_id="d5"),
        lambda m: _send_into(sent, m),
    )
    await handler.handle_event(
        installation, _message_event("not-a-date", dedup_id="d6"), lambda m: _send_into(sent, m)
    )
    assert "กำหนดส่ง" in _last(sent).content

    state = await discord_harness.conversation_states.get(installation.id, _USER)
    assert state is not None and state.step == "due_date"


@pytest.mark.asyncio
async def test_my_tasks_shows_assigned_items(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    _, instance, installation, _ = await _linked_setup(discord_harness)
    discord_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    work_item, _ = await discord_harness.prom_api_client.create_work_item(
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
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _message_event("งานของฉัน"), lambda m: _send_into(sent, m)
    )
    assert "ENG-" in _last(sent).content
    assert work_item.name in _last(sent).content


@pytest.mark.asyncio
async def test_selecting_my_tasks_item_starts_update_flow_and_mark_complete(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    _, instance, installation, _ = await _linked_setup(discord_harness)
    discord_harness.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="done", name="Done", group="completed")
    ]
    work_item, _ = await discord_harness.prom_api_client.create_work_item(
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
    handler = discord_harness.handler

    # The my-tasks Select's option value is "{project_id}:{work_item_id}" (see components.py's
    # build_my_tasks_list docstring for why — a Select option has only one value field, unlike
    # LINE's postback data string which can carry two).
    await handler.handle_event(
        installation,
        _component_event("select_work_item", f"p1:{work_item.id}", dedup_id="u1"),
        lambda m: _send_into(sent, m),
    )
    assert _component_ids(_last(sent)) == {"select_update_action", "cancel"}

    await handler.handle_event(
        installation,
        _component_event("select_update_action", "complete", dedup_id="u2"),
        lambda m: _send_into(sent, m),
    )
    assert _component_ids(_last(sent)) == {"confirm", "cancel"}

    await handler.handle_event(
        installation, _component_event("confirm", dedup_id="u3"), lambda m: _send_into(sent, m)
    )
    assert "สำเร็จ" in _last(sent).content
    assert work_item.state_id == "done"
    assert await discord_harness.conversation_states.get(installation.id, _USER) is None


@pytest.mark.asyncio
async def test_workspace_selection_when_multiple_workspaces_and_no_default(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    customer = await discord_harness.seed_customer()
    instance = await discord_harness.seed_prom_instance(customer.id)
    installation = await discord_harness.seed_installation(customer.id)
    ws_a = await discord_harness.seed_workspace(customer.id, instance.id)
    ws_b = await discord_harness.seed_workspace(customer.id, instance.id)
    await discord_harness.link_workspace(installation.id, ws_a.id)
    await discord_harness.link_workspace(installation.id, ws_b.id)
    await discord_harness.link_user(
        customer_id=customer.id,
        installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id="prom-user-1",
        channel_user_id=_USER,
        default_workspace_connection_id=None,
    )
    discord_harness.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _message_event("งานของฉัน", dedup_id="ws1"), lambda m: _send_into(sent, m)
    )
    picker = _last(sent)
    assert picker.select is not None and picker.select.custom_id == "select_workspace"
    chosen_workspace_id = picker.select.options[0].value

    await handler.handle_event(
        installation,
        _component_event("select_workspace", chosen_workspace_id, dedup_id="ws2"),
        lambda m: _send_into(sent, m),
    )
    # Resumes the originally-requested action (my_tasks) once a workspace is chosen.
    assert "ไม่พบงานที่ได้รับมอบหมาย" in _last(sent).content or "ENG-" in _last(sent).content


@pytest.mark.asyncio
async def test_stale_component_with_no_conversation_state_replies_gracefully(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    """A button/select interaction can arrive after its conversation state already expired (TTL)
    or after a process restart (Views aren't persisted across restarts — see
    gateway_client.py's module docstring). Must not crash; must not silently do nothing either."""
    _, _, installation, _ = await _linked_setup(discord_harness)
    handler = discord_harness.handler

    await handler.handle_event(
        installation, _component_event("select_project", "p1"), lambda m: _send_into(sent, m)
    )

    assert len(sent) == 1
    assert "เซสชันหมดอายุ" in _last(sent).content


@pytest.mark.asyncio
async def test_selecting_update_action_does_not_execute_anything_by_itself(
    discord_harness: DiscordHandlerHarness, sent: list[DiscordMessage]
) -> None:
    """Choosing "state" from the update-action menu only shows a state picker — it must not call
    UpdateWorkItemState until a specific state is actually selected. (State-change has no separate
    "are you sure?" confirm step of its own — selecting the target state IS the confirmation,
    same as LINE's Gate 5 handler; MarkComplete is the one action with an explicit extra confirm
    step, since it needs no further input to disambiguate intent from a single tap.)"""
    _, instance, installation, _ = await _linked_setup(discord_harness)
    discord_harness.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="s2", name="In Progress", group="started")
    ]
    work_item, _ = await discord_harness.prom_api_client.create_work_item(
        instance.base_url,
        "delegated-token",
        "acme",
        "p1",
        name="Task",
        state_id="s1",
        assignee_ids=[],
        target_date=None,
        description_html=None,
        external_id="seed3",
        external_source="test",
    )
    handler = discord_harness.handler

    await handler.handle_event(
        installation,
        _component_event("select_work_item", f"p1:{work_item.id}", dedup_id="cf1"),
        lambda m: _send_into(sent, m),
    )
    await handler.handle_event(
        installation,
        _component_event("select_update_action", "state", dedup_id="cf2"),
        lambda m: _send_into(sent, m),
    )
    assert work_item.state_id == "s1"  # unchanged — only a picker was shown, nothing confirmed yet

    await handler.handle_event(
        installation,
        _component_event("select_state", "s2", dedup_id="cf3"),
        lambda m: _send_into(sent, m),
    )
    # UpdateFlowStep.AWAIT_STATE applies the change immediately on selection (no separate
    # confirm step for state — mirrors LINE's Gate 5 behavior exactly, see
    # UpdateWorkItemFlow.confirm_state_change being called straight from the select handler).
    assert work_item.state_id == "s2"
