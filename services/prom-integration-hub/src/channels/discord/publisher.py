"""Implements the application's MessagePublisher port for Discord — used by
SendPrivateNotification (Gate 7's eventual entry point), not by the live bot's own same-turn
replies (those go through discord.py's own send/response methods with an already-open connection,
see src/channels/discord/gateway_client.py). Sibling to src/channels/line/publisher.py.
"""

from __future__ import annotations

from src.application.notifications.ports import MessagePublisher
from src.channels.discord.rest import send_dm
from src.domain.integrations.entities import ChannelType
from src.shared.errors import ChannelDeliveryFailedError


class DiscordMessagePublisher(MessagePublisher):
    async def send_private_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        channel_user_id: str,
        text: str,
    ) -> None:
        if channel_type is not ChannelType.DISCORD:
            raise ChannelDeliveryFailedError(
                f"DiscordMessagePublisher cannot send to {channel_type}"
            )
        bot_token = credentials.get("bot_token", "")
        await send_dm(bot_token, channel_user_id, text)
