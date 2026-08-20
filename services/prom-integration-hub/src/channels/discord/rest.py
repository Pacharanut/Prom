"""Raw Discord REST API calls needed outside any live Gateway connection — specifically, sending
a DM to a user with only a bot token in hand (e.g. from a future Celery task reacting to a Prom
webhook, Gate 7's eventual `SendPrivateNotification` caller). Deliberately NOT built on discord.py's
`Client`/Gateway machinery: that requires an open WebSocket connection this process doesn't have
outside `apps/discord_bot/main.py`'s own long-running bot process. Mirrors
`src/channels/line/client.py`'s shape (plain httpx, injectable client for tests) for the same
reason LINE's push capability doesn't depend on an active webhook request.

The live Gateway client (`src/channels/discord/gateway_client.py`) does NOT use this module for
its own same-turn replies — those go through discord.py's own `message.channel.send`/
`interaction.response.send_message`, which already has a live connection's HTTP session to reuse.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.shared.errors import ChannelDeliveryFailedError

_API_BASE = "https://discord.com/api/v10"
_REQUEST_TIMEOUT_SECONDS = 10.0


async def _request(
    method: str,
    path: str,
    bot_token: str,
    *,
    json_body: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await http_client.request(
                method,
                f"{_API_BASE}{path}",
                json=json_body,
                headers={
                    "Authorization": f"Bot {bot_token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ChannelDeliveryFailedError(f"could not reach Discord API: {exc}") from exc
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code >= 300:
        raise ChannelDeliveryFailedError(
            f"Discord API rejected the request (HTTP {response.status_code}): {response.text[:500]}"
        )
    body: dict[str, Any] = response.json() if response.content else {}
    return body


async def create_dm_channel(
    bot_token: str, recipient_user_id: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """POST /users/@me/channels — opens (or fetches the existing) DM channel with a user. Returns
    the DM channel id, needed for send_message."""
    body = await _request(
        "POST",
        "/users/@me/channels",
        bot_token,
        json_body={"recipient_id": recipient_user_id},
        client=client,
    )
    channel_id = body.get("id")
    if not channel_id:
        raise ChannelDeliveryFailedError("Discord API did not return a DM channel id")
    return str(channel_id)


async def send_message(
    bot_token: str, channel_id: str, content: str, *, client: httpx.AsyncClient | None = None
) -> None:
    """POST /channels/{channel_id}/messages — plain text only (no components); this is the push
    path, there is no interaction/button context to attach one to."""
    await _request(
        "POST",
        f"/channels/{channel_id}/messages",
        bot_token,
        json_body={"content": content},
        client=client,
    )


async def send_dm(
    bot_token: str, recipient_user_id: str, content: str, *, client: httpx.AsyncClient | None = None
) -> None:
    channel_id = await create_dm_channel(bot_token, recipient_user_id, client=client)
    await send_message(bot_token, channel_id, content, client=client)
