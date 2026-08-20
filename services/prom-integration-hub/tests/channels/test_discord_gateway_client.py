"""src/channels/discord/gateway_client.py — the one module that touches real discord.py runtime
objects. Tested with minimal duck-typed stand-ins (a real discord.py Message/Interaction needs a
live connection's internal state to construct), covering exactly the filtering logic that lives at
this layer and nowhere else: bot messages ignored, non-DM messages ignored, a legitimate DM routes
through to the handler and its reply gets sent. DiscordWebhookHandler's own conversation logic is
already thoroughly covered by tests/channels/test_discord_handler.py — this file does not
duplicate that, it only proves the discord.py glue wires correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import discord
import pytest

from src.channels.discord.components import DiscordMessage
from src.channels.discord.events import DiscordEvent
from src.channels.discord.gateway_client import build_client
from src.channels.discord.handler import DiscordWebhookHandler
from src.domain.integrations.entities import ChannelInstallation, ChannelType, InstallationStatus


def _installation() -> ChannelInstallation:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return ChannelInstallation(
        id=uuid4(),
        customer_id=uuid4(),
        channel_type=ChannelType.DISCORD,
        display_name="Acme Bot",
        status=InstallationStatus.ACTIVE,
        external_account_id="bot-1",
        encrypted_credentials=b"",
        default_workspace_connection_id=None,
        created_at=now,
        updated_at=now,
    )


class _FakeUser:
    def __init__(self, user_id: int, *, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot


class _FakeChannel:
    def __init__(self, channel_type: discord.ChannelType) -> None:
        self.type = channel_type
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class _FakeMessage:
    def __init__(
        self, author: _FakeUser, channel: _FakeChannel, content: str, message_id: int
    ) -> None:
        self.author = author
        self.channel = channel
        self.content = content
        self.id = message_id


class _RecordingHandler(DiscordWebhookHandler):  # type: ignore[misc]
    """Bypasses DiscordWebhookHandler's real __init__ (this test doesn't need any of its ports —
    it only needs something whose handle_event records what it was called with and sends one
    fixed reply, to prove gateway_client's extraction/wiring reached the handler correctly."""

    def __init__(self) -> None:
        self.calls: list[tuple[ChannelInstallation, DiscordEvent]] = []

    async def handle_event(  # type: ignore[override]
        self, installation: ChannelInstallation, event: DiscordEvent, send: Any
    ) -> None:
        self.calls.append((installation, event))
        await send(DiscordMessage(content="ok"))


@pytest.mark.asyncio
async def test_bot_authored_message_is_ignored() -> None:
    installation = _installation()
    handler = _RecordingHandler()
    client = build_client(installation, lambda: _async_return(handler))

    channel = _FakeChannel(discord.ChannelType.private)
    message = _FakeMessage(_FakeUser(1, bot=True), channel, "เมนู", 100)
    await client.on_message(message)  # type: ignore[attr-defined]

    assert handler.calls == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_non_dm_message_is_ignored() -> None:
    installation = _installation()
    handler = _RecordingHandler()
    client = build_client(installation, lambda: _async_return(handler))

    channel = _FakeChannel(discord.ChannelType.text)
    message = _FakeMessage(_FakeUser(1), channel, "เมนู", 101)
    await client.on_message(message)  # type: ignore[attr-defined]

    assert handler.calls == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_dm_message_reaches_handler_and_reply_is_sent() -> None:
    installation = _installation()
    handler = _RecordingHandler()
    client = build_client(installation, lambda: _async_return(handler))

    channel = _FakeChannel(discord.ChannelType.private)
    message = _FakeMessage(_FakeUser(42), channel, "เมนู", 102)
    await client.on_message(message)  # type: ignore[attr-defined]

    assert len(handler.calls) == 1
    called_installation, event = handler.calls[0]
    assert called_installation is installation
    assert event.kind == "message"
    assert event.user_id == "42"
    assert event.text == "เมนู"
    assert event.dedup_id == "102"

    assert channel.sent == [{"content": "ok"}]


async def _async_return(value: DiscordWebhookHandler) -> DiscordWebhookHandler:
    return value
