"""Phase 2D through the real controllers: the bot is silent in groups except for its commands,
binding needs a linked user, forged postbacks can't bind other workspaces, and group messages
are never treated as private chats."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.application.groups.service import GroupBindingService
from src.application.work_items.use_cases import ListProjects
from src.channels.discord.events import DiscordEvent
from src.channels.line.postback import decode_postback, encode_postback
from src.domain.groups.entities import GroupEventType
from src.domain.work_items.entities import ProjectRef
from tests.channels.conftest import DiscordHandlerHarness, LineHandlerHarness
from tests.fakes.repositories import FakeGroupBindingRepository

_USER = "U-group-user"
_GROUP = "C-group-1"


def _service(
    h: LineHandlerHarness | DiscordHandlerHarness, repo: FakeGroupBindingRepository
) -> GroupBindingService:
    return GroupBindingService(
        bindings=repo,
        list_projects=ListProjects(
            prom_instances=h.prom_instances,
            delegated_credentials=h.delegated_credentials,
            secrets_provider=h.secrets_provider,
            prom_api_client=h.prom_api_client,
            audit_log=h.audit_log,
            clock=h.clock,
        ),
        audit_log=h.audit_log,
        clock=h.clock,
    )


async def _seed(h: LineHandlerHarness | DiscordHandlerHarness, *, link_user: bool = True) -> Any:
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    workspace = await h.seed_workspace(customer.id, instance.id)
    installation = await h.seed_installation(customer.id)
    await h.link_workspace(installation.id, workspace.id)
    if link_user:
        await h.link_user(
            customer_id=customer.id,
            installation_id=installation.id,
            prom_instance_id=instance.id,
            prom_user_id="u-me",
            channel_user_id=_USER,
            default_workspace_connection_id=None,
        )
    h.prom_api_client.projects = [ProjectRef(id="p1", name="AI Platform", identifier="AI")]
    return installation, workspace


def _group_event(kind: str, value: str, n: int, *, user: str | None = _USER) -> dict[str, Any]:
    source: dict[str, Any] = {"type": "group", "groupId": _GROUP}
    if user:
        source["userId"] = user
    base: dict[str, Any] = {
        "replyToken": f"r{n}",
        "webhookEventId": f"g{n}",
        "timestamp": n,
        "source": source,
    }
    if kind == "message":
        base.update(type="message", message={"type": "text", "id": f"m{n}", "text": value})
    elif kind == "postback":
        base.update(type="postback", postback={"data": value})
    else:
        base.update(type="join")
    return {"events": [base]}


def _quick_reply_datas(sent: list) -> list[dict[str, str]]:
    return [decode_postback(i["action"]["data"]) for i in sent[-1][2][0]["quickReply"]["items"]]


@pytest.mark.asyncio
async def test_join_greets_and_chatter_is_ignored(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    installation, _ = await _seed(line_harness)
    handler = replace(
        line_harness.handler, groups=_service(line_harness, FakeGroupBindingRepository())
    )

    await handler.handle_payload(installation, "t", _group_event("join", "", 1, user=None))
    assert "ผูกกลุ่ม" in sent_messages[-1][2][0]["text"]
    count = len(sent_messages)

    for n, text in enumerate(["เมนู", "งานของฉันวันนี้", "สร้างงาน", "hello team"], start=2):
        await handler.handle_payload(installation, "t", _group_event("message", text, n))
    assert len(sent_messages) == count  # no menu, no AI, no create flow inside the group


@pytest.mark.asyncio
async def test_group_is_silent_when_feature_not_wired(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    installation, _ = await _seed(line_harness)
    await line_harness.handler.handle_payload(installation, "t", _group_event("message", "เมนู", 1))
    assert sent_messages == []


@pytest.mark.asyncio
async def test_unlinked_user_cannot_bind(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    installation, _ = await _seed(line_harness, link_user=False)
    handler = replace(
        line_harness.handler, groups=_service(line_harness, FakeGroupBindingRepository())
    )
    await handler.handle_payload(installation, "t", _group_event("message", "ผูกกลุ่ม", 1))
    assert "ต้องผูกบัญชี" in sent_messages[-1][2][0]["text"]


@pytest.mark.asyncio
async def test_bind_toggle_unbind_flow(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    installation, workspace = await _seed(line_harness)
    repo = FakeGroupBindingRepository()
    handler = replace(line_harness.handler, groups=_service(line_harness, repo))

    await handler.handle_payload(installation, "t", _group_event("message", "ผูกกลุ่ม", 1))
    options = _quick_reply_datas(sent_messages)
    assert [o["value"] for o in options] == ["all", "p1"]
    assert all(o["action"] == "group_bind" and o["ws"] == str(workspace.id) for o in options)

    await handler.handle_payload(
        installation,
        "t",
        _group_event(
            "postback", encode_postback("group_bind", value="p1", ws=str(workspace.id)), 2
        ),
    )
    assert "ผูกกลุ่มนี้กับ Project AI Platform แล้ว" in sent_messages[-1][2][0]["text"]
    binding = await repo.get_by_group(installation.id, _GROUP)
    assert binding is not None and binding.project_id == "p1"

    await handler.handle_payload(
        installation,
        "t",
        _group_event("postback", encode_postback("group_toggle", value="assigned"), 3),
    )
    binding = await repo.get_by_group(installation.id, _GROUP)
    assert binding is not None and GroupEventType.ASSIGNED in binding.events

    await handler.handle_payload(installation, "t", _group_event("message", "ยกเลิกผูกกลุ่ม", 4))
    assert await repo.get_by_group(installation.id, _GROUP) is None


@pytest.mark.asyncio
async def test_forged_workspace_in_postback_is_ignored(
    line_harness: LineHandlerHarness, sent_messages: list
) -> None:
    from uuid import uuid4

    installation, _ = await _seed(line_harness)
    repo = FakeGroupBindingRepository()
    handler = replace(line_harness.handler, groups=_service(line_harness, repo))
    await handler.handle_payload(
        installation,
        "t",
        _group_event("postback", encode_postback("group_bind", value="all", ws=str(uuid4())), 1),
    )
    assert await repo.get_by_group(installation.id, _GROUP) is None
    assert sent_messages == []


@pytest.mark.asyncio
async def test_discord_channel_bind_via_select(discord_harness: DiscordHandlerHarness) -> None:
    installation, workspace = await _seed(discord_harness)
    repo = FakeGroupBindingRepository()
    handler = replace(discord_harness.handler, groups=_service(discord_harness, repo))
    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    await handler.handle_event(
        installation,
        DiscordEvent(
            kind="message",
            user_id=_USER,
            text="ผูกกลุ่ม",
            component_custom_id=None,
            component_value=None,
            dedup_id="m1",
            group_id="chan-1",
        ),
        send,
    )
    assert sent[-1].select is not None and sent[-1].select.custom_id == "group_bind"
    assert sent[-1].select.options[0].value == f"{workspace.id}:all"

    await handler.handle_event(
        installation,
        DiscordEvent(
            kind="component",
            user_id=_USER,
            text=None,
            component_custom_id="group_bind",
            component_value=f"{workspace.id}:all",
            dedup_id="i1",
            group_id="chan-1",
        ),
        send,
    )
    binding = await repo.get_by_group(installation.id, "chan-1")
    assert binding is not None and binding.project_id is None
    assert "ทุก Project" in sent[-1].content
