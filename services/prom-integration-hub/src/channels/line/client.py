"""LINE Messaging API HTTP client — reply and push. Sibling to validator.py's credential-check
call, same injectable-client pattern for tests.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.shared.errors import ChannelDeliveryFailedError

_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_REQUEST_TIMEOUT_SECONDS = 10.0


async def _post(
    url: str, channel_access_token: str, body: dict[str, Any], *, client: httpx.AsyncClient | None
) -> None:
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await http_client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {channel_access_token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ChannelDeliveryFailedError(f"could not reach LINE API: {exc}") from exc
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code >= 300:
        raise ChannelDeliveryFailedError(
            f"LINE API rejected the request (HTTP {response.status_code}): {response.text[:500]}"
        )


async def reply_message(
    channel_access_token: str,
    reply_token: str,
    messages: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """POST /v2/bot/message/reply — used within the webhook's own request/response cycle, while
    the reply_token from that same event is still valid (LINE reply tokens are single-use and
    short-lived)."""
    await _post(
        _REPLY_URL,
        channel_access_token,
        {"replyToken": reply_token, "messages": messages},
        client=client,
    )


async def push_message(
    channel_access_token: str,
    to: str,
    messages: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """POST /v2/bot/message/push — used outside any webhook request/response cycle (e.g. Gate 7
    Prom-event-driven notifications), or as a second message within the same turn once the single
    reply_token has already been spent."""
    await _post(_PUSH_URL, channel_access_token, {"to": to, "messages": messages}, client=client)
