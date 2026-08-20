"""Implements the application's MessagePublisher port by dispatching to the LINE or Discord
publisher based on channel_type. Pure routing — no business logic of its own. Sibling to
validator_registry.py; added in Gate 6 once a second channel publisher existed to dispatch to
(Gate 5 left SendPrivateNotification wired to nothing yet, since LineMessagePublisher alone didn't
need a registry — see its class docstring)."""

from __future__ import annotations

from src.application.notifications.ports import MessagePublisher
from src.channels.discord.publisher import DiscordMessagePublisher
from src.channels.line.publisher import LineMessagePublisher
from src.domain.integrations.entities import ChannelType
from src.shared.errors import ChannelDeliveryFailedError


class MessagePublisherRegistry(MessagePublisher):
    def __init__(self) -> None:
        self._line = LineMessagePublisher()
        self._discord = DiscordMessagePublisher()

    async def send_private_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        channel_user_id: str,
        text: str,
    ) -> None:
        if channel_type is ChannelType.LINE:
            await self._line.send_private_message(channel_type, credentials, channel_user_id, text)
        elif channel_type is ChannelType.DISCORD:
            await self._discord.send_private_message(
                channel_type, credentials, channel_user_id, text
            )
        else:
            raise ChannelDeliveryFailedError(f"no MessagePublisher for {channel_type}")
