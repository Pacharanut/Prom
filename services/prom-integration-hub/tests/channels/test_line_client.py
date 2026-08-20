"""LINE Messaging API reply/push, using httpx.MockTransport (no real network call), same pattern
as tests/channels/test_line_validator.py."""

from __future__ import annotations

import httpx
import pytest

from src.channels.line.client import push_message, reply_message
from src.shared.errors import ChannelDeliveryFailedError


@pytest.mark.asyncio
async def test_reply_message_posts_reply_token_and_messages() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await reply_message(
            "token-123", "reply-tok", [{"type": "text", "text": "hi"}], client=client
        )

    assert captured["url"] == "https://api.line.me/v2/bot/message/reply"
    assert captured["auth"] == "Bearer token-123"


@pytest.mark.asyncio
async def test_push_message_posts_to_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.line.me/v2/bot/message/push"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await push_message("token-123", "U123", [{"type": "text", "text": "hi"}], client=client)


@pytest.mark.asyncio
async def test_non_2xx_raises_channel_delivery_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChannelDeliveryFailedError):
            await reply_message(
                "token-123", "reply-tok", [{"type": "text", "text": "hi"}], client=client
            )


@pytest.mark.asyncio
async def test_network_error_raises_channel_delivery_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChannelDeliveryFailedError):
            await push_message("token-123", "U123", [{"type": "text", "text": "hi"}], client=client)
