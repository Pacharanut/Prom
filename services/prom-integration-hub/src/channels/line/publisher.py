"""Implements the application's MessagePublisher port for LINE — used by
SendPrivateNotification (Gate 7's eventual entry point), not by the webhook handler's own
same-request replies (those call src/channels/line/client.py directly, see handler.py's
module docstring for why)."""

from __future__ import annotations

from src.application.notifications.ports import MessagePublisher
from src.channels.line.client import push_message
from src.domain.integrations.entities import ChannelType
from src.shared.errors import ChannelDeliveryFailedError


class LineMessagePublisher(MessagePublisher):
    async def send_private_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        channel_user_id: str,
        text: str,
    ) -> None:
        if channel_type is not ChannelType.LINE:
            raise ChannelDeliveryFailedError(f"LineMessagePublisher cannot send to {channel_type}")
        access_token = credentials.get("channel_access_token", "")
        await push_message(access_token, channel_user_id, [{"type": "text", "text": text}])

    async def send_group_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        group_id: str,
        text: str,
    ) -> None:
        """Phase 2D: the same push API accepts a groupId/roomId as `to`."""
        if channel_type is not ChannelType.LINE:
            raise ChannelDeliveryFailedError(f"LineMessagePublisher cannot send to {channel_type}")
        access_token = credentials.get("channel_access_token", "")
        await push_message(access_token, group_id, [{"type": "text", "text": text}])
