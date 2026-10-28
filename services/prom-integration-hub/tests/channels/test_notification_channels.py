"""Phase 2B through the real controllers: "สรุปงาน" / "ตั้งค่าแจ้งเตือน" keywords and menu
buttons, toggling a switch, and the AI shortcut routing to the same panels."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from src.application.notifications.panel import NotificationsPanel
from src.application.notifications.preferences import (
    GetNotificationPreferences,
    UpdateNotificationPreferences,
)
from src.application.notifications.summary import BuildDailySummary
from src.application.work_items.use_cases import (
    FindBlockedWorkItems,
    ListMyWorkItems,
    ListProjects,
)
from src.channels.discord.events import DiscordEvent
from src.channels.line.postback import decode_postback, encode_postback
from src.domain.notifications.entities import NotificationEventType
from src.domain.work_items.entities import ProjectRef, WorkItem
from tests.channels.conftest import DiscordHandlerHarness, LineHandlerHarness
from tests.fakes.repositories import (
    FakeNotificationPreferenceRepository,
    FakeOutboundMessageRepository,
)

_USER = "U-notif"


def _panel(
    h: LineHandlerHarness | DiscordHandlerHarness, prefs: FakeNotificationPreferenceRepository
) -> NotificationsPanel:
    def uc(cls: Any) -> Any:
        return cls(
            prom_instances=h.prom_instances,
            delegated_credentials=h.delegated_credentials,
            secrets_provider=h.secrets_provider,
            prom_api_client=h.prom_api_client,
            audit_log=h.audit_log,
            clock=h.clock,
        )

    return NotificationsPanel(
        get_preferences=GetNotificationPreferences(
            linked_identities=h.linked_identities, notification_preferences=prefs
        ),
        update_preferences=UpdateNotificationPreferences(
            linked_identities=h.linked_identities,
            notification_preferences=prefs,
            audit_log=h.audit_log,
            clock=h.clock,
        ),
        build_daily_summary=BuildDailySummary(
            list_projects=uc(ListProjects),
            list_my_work_items=uc(ListMyWorkItems),
            find_blocked_work_items=uc(FindBlockedWorkItems),
            outbound_messages=FakeOutboundMessageRepository(),
            notification_preferences=prefs,
            clock=h.clock,
        ),
    )


async def _seed(h: LineHandlerHarness | DiscordHandlerHarness) -> Any:
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
        channel_user_id=_USER,
        default_workspace_connection_id=None,  # resolved through the installation's only link
    )
    h.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]
    today = h.clock.now().date()
    h.prom_api_client.work_items["w1"] = WorkItem(
        id="w1",
        sequence_id=1,
        project_id="p1",
        name="Ship it",
        state_id=None,
        priority=None,
        assignee_ids=["u-me"],
        target_date=today - timedelta(days=1),
        completed_at=None,
    )
    return installation


def _event(kind: str, text: str, n: int) -> dict[str, Any]:
    base: dict[str, Any] = {
        "replyToken": f"r{n}",
        "webhookEventId": f"e{n}",
        "timestamp": n,
        "source": {"type": "user", "userId": _USER},
    }
    if kind == "message":
        base.update(type="message", message={"type": "text", "id": f"m{n}", "text": text})
    else:
        base.update(type="postback", postback={"data": text})
    return {"events": [base]}


@pytest.mark.asyncio
async def test_line_summary_keyword(line_harness: LineHandlerHarness, sent_messages: list) -> None:
    installation = await _seed(line_harness)
    handler = replace(
        line_harness.handler,
        notifications=_panel(line_harness, FakeNotificationPreferenceRepository()),
    )
    await handler.handle_payload(installation, "t", _event("message", "สรุปงาน", 1))
    text = sent_messages[-1][2][0]["text"]
    assert "สรุปงานของคุณ" in text and "ENG-1: Ship it" in text and "เลยกำหนด (1)" in text


@pytest.mark.asyncio
async def test_line_settings_panel_and_toggle(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    installation = await _seed(line_harness)
    prefs = FakeNotificationPreferenceRepository()
    handler = replace(line_harness.handler, notifications=_panel(line_harness, prefs))

    await handler.handle_payload(installation, "t", _event("message", "ตั้งค่าแจ้งเตือน", 1))
    panel = sent_messages[-1][2][0]
    items = panel["quickReply"]["items"]
    assert len(items) == 9
    assert all(len(i["action"]["label"]) <= 20 for i in items)
    assert {decode_postback(i["action"]["data"])["action"] for i in items} == {"notif_toggle"}

    await handler.handle_payload(
        installation, "t", _event("postback", encode_postback("notif_toggle", value="overdue"), 2)
    )
    assert "⛔ เลยกำหนด" in sent_messages[-1][2][0]["text"]
    identity = await line_harness.linked_identities.get_active_by_channel_user(
        installation.id, _USER
    )
    assert identity is not None
    assert await prefs.is_enabled(identity.id, NotificationEventType.OVERDUE) is False


@pytest.mark.asyncio
async def test_line_menu_buttons_open_panels(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    installation = await _seed(line_harness)
    handler = replace(
        line_harness.handler,
        notifications=_panel(line_harness, FakeNotificationPreferenceRepository()),
    )
    await handler.handle_payload(
        installation, "t", _event("postback", encode_postback("summary"), 1)
    )
    assert "สรุปงานของคุณ" in sent_messages[-1][2][0]["text"]
    await handler.handle_payload(
        installation, "t", _event("postback", encode_postback("notif_settings"), 2)
    )
    assert "ตั้งค่าการแจ้งเตือน" in sent_messages[-1][2][0]["text"]


@pytest.mark.asyncio
async def test_discord_toggle_select(discord_harness: DiscordHandlerHarness) -> None:
    installation = await _seed(discord_harness)
    prefs = FakeNotificationPreferenceRepository()
    handler = replace(discord_harness.handler, notifications=_panel(discord_harness, prefs))
    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    await handler.handle_event(
        installation,
        DiscordEvent(
            kind="component",
            user_id=_USER,
            text=None,
            component_custom_id="menu_notif_settings",
            component_value=None,
            dedup_id="i1",
        ),
        send,
    )
    assert sent[-1].select is not None and sent[-1].select.custom_id == "notif_toggle"
    assert len(sent[-1].select.options) == 9

    await handler.handle_event(
        installation,
        DiscordEvent(
            kind="component",
            user_id=_USER,
            text=None,
            component_custom_id="notif_toggle",
            component_value="daily_summary",
            dedup_id="i2",
        ),
        send,
    )
    assert "⛔ สรุปงานประจำวัน" in sent[-1].content
