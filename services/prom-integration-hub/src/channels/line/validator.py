"""Validates LINE channel credentials by calling LINE's own "bot info" endpoint. This is a
credential-validation adapter only — full webhook handling, signature verification, and
conversation flow are Gate 5 scope.
"""

from __future__ import annotations

import httpx

_LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"
_REQUEST_TIMEOUT_SECONDS = 10.0


async def validate_line_credentials(
    channel_access_token: str, *, client: httpx.AsyncClient | None = None
) -> tuple[bool, str | None, str | None]:
    """Returns (is_valid, external_account_id, error_message).

    LINE's GET /v2/bot/info response body includes `basicId` (the channel's stable @-handle,
    e.g. "@abcde") and `userId` (the bot's LINE user id) when the token is valid — `basicId` is
    used as external_account_id since it's the identifier LINE surfaces to admins/users, per
    LINE's Messaging API reference for this endpoint.

    `client` is injectable for tests (httpx.AsyncClient(transport=httpx.MockTransport(...))); it
    defaults to a real client when not provided.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await http_client.get(
                _LINE_BOT_INFO_URL,
                headers={"Authorization": f"Bearer {channel_access_token}"},
            )
        except httpx.HTTPError as exc:
            return False, None, f"could not reach LINE API: {exc}"
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        return False, None, f"LINE API rejected the credentials (HTTP {response.status_code})"

    body = response.json()
    external_account_id = str(body.get("basicId") or body.get("userId") or "")
    if not external_account_id:
        return False, None, "LINE API response did not include an account identifier"
    return True, external_account_id, None
