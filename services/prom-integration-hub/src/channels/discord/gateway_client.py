"""Wires a discord.py Gateway `Client` to one ChannelInstallation: converts real discord.py
`Message`/`Interaction` objects into a `DiscordEvent` plus a bound `send` closure, and drives
`DiscordWebhookHandler`. This is the *only* module in Gate 6 that imports discord.py's runtime
types directly (besides the composition root in apps/discord_bot/main.py) — DiscordWebhookHandler
and everything it depends on stays free of discord.py specifics, which is what lets
tests/channels/test_discord_handler.py drive the handler with plain duck-typed fakes instead of a
live Gateway connection.

Each inbound event gets a *fresh* DiscordWebhookHandler built from `handler_factory` (which opens
a short-lived DB session per call) — a single Client instance runs for the process's entire
lifetime, but no DB session is held open that long; see apps/discord_bot/main.py's module
docstring for why session-per-operation matters here.

Views built here are ordinary (not `client.add_view()`-registered) — their button/select callbacks
are Python closures that only exist for the lifetime of the process that sent them. If the bot
process restarts mid-conversation, a button/select the user still sees in Discord will fail to
respond (interaction times out) rather than resuming the flow. This is an accepted Phase 1
limitation, consistent with "no hot-reload of installations" (README) — conversation state itself
already has a 10-minute TTL, so a mid-flow restart is a narrow window, not a durability guarantee
this gate makes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import discord

from src.channels.discord.components import DiscordMessage
from src.channels.discord.events import DiscordEvent
from src.channels.discord.handler import DiscordWebhookHandler, SendFn
from src.domain.integrations.entities import ChannelInstallation
from src.shared.errors import ChannelDeliveryFailedError

HandlerFactory = Callable[[], Awaitable[DiscordWebhookHandler]]
"""Builds one DiscordWebhookHandler bound to a fresh, short-lived DB session — called once per
inbound event, never once at startup."""

_BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "danger": discord.ButtonStyle.danger,
}
_SELECT_PLACEHOLDER_MAX_LEN = 150
_SELECT_OPTION_LABEL_MAX_LEN = 100


def build_client(
    installation: ChannelInstallation, handler_factory: HandlerFactory
) -> discord.Client:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.dm_messages = True
    intents.message_content = True
    """Privileged intent — must be explicitly enabled for the bot application in the Discord
    Developer Portal ("Message Content Intent" under Bot settings), or DM text will arrive empty.
    Documented in README's "Discord bot setup" section."""

    client = discord.Client(intents=intents)

    def _make_view(out: DiscordMessage) -> discord.ui.View | None:
        if not out.buttons and out.select is None:
            return None
        view = discord.ui.View(timeout=None)

        for b in out.buttons:
            button: discord.ui.Button[Any] = discord.ui.Button(
                label=b.label,
                custom_id=b.custom_id,
                style=_BUTTON_STYLES.get(b.style, discord.ButtonStyle.primary),
            )
            # discord.py's documented pattern for dynamically-built views (no subclassing a View
            # per message shape) is exactly this: assign a plain async closure to `.callback`.
            # The stub types `.callback` as a bound method, so mypy strict flags the assignment —
            # a known friction point, not a real type-safety gap; this is the library's own
            # sanctioned dynamic-item API.
            button.callback = _make_component_callback(  # type: ignore[method-assign,assignment]
                b.custom_id, is_select=False
            )
            view.add_item(button)

        if out.select is not None:
            spec = out.select
            select: discord.ui.Select[Any] = discord.ui.Select(
                custom_id=spec.custom_id,
                placeholder=spec.placeholder[:_SELECT_PLACEHOLDER_MAX_LEN],
                options=[
                    discord.SelectOption(
                        label=o.label[:_SELECT_OPTION_LABEL_MAX_LEN], value=o.value
                    )
                    for o in spec.options
                ],
            )
            select.callback = _make_component_callback(  # type: ignore[method-assign,assignment]
                spec.custom_id, is_select=True
            )
            view.add_item(select)

        return view

    def _make_component_callback(
        custom_id: str, *, is_select: bool
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            value: str | None = None
            if is_select and interaction.data is not None:
                values_obj = interaction.data.get("values")
                if isinstance(values_obj, list) and values_obj:
                    value = str(values_obj[0])

            event = DiscordEvent(
                kind="component",
                user_id=str(interaction.user.id),
                text=None,
                component_custom_id=custom_id,
                component_value=value,
                dedup_id=str(interaction.id),
            )
            handler = await handler_factory()
            await handler.handle_event(installation, event, _send_for_interaction(interaction))

        return _callback

    def _send_for_interaction(interaction: discord.Interaction) -> SendFn:
        async def _send(out: DiscordMessage) -> None:
            view = _make_view(out)
            kwargs: dict[str, Any] = {"content": out.content}
            if view is not None:
                kwargs["view"] = view
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(**kwargs)
                else:
                    await interaction.followup.send(**kwargs)
            except discord.HTTPException as exc:
                raise ChannelDeliveryFailedError(
                    f"discord interaction response failed: {exc}"
                ) from exc

        return _send

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.channel.type is not discord.ChannelType.private:
            return

        event = DiscordEvent(
            kind="message",
            user_id=str(message.author.id),
            text=message.content,
            component_custom_id=None,
            component_value=None,
            dedup_id=str(message.id),
        )
        handler = await handler_factory()

        async def _send(out: DiscordMessage) -> None:
            view = _make_view(out)
            kwargs: dict[str, Any] = {"content": out.content}
            if view is not None:
                kwargs["view"] = view
            try:
                await message.channel.send(**kwargs)
            except discord.HTTPException as exc:
                raise ChannelDeliveryFailedError(f"discord message send failed: {exc}") from exc

        await handler.handle_event(installation, event, _send)

    return client
