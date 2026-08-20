"""Validates Discord bot credentials by calling Discord's own "current user" endpoint. This is a
credential-validation adapter only — full interaction/event handling, signature verification, and
command flow are Gate 6 scope.
"""

from __future__ import annotations

import httpx

_DISCORD_ME_URL = "https://discord.com/api/v10/users/@me"
_REQUEST_TIMEOUT_SECONDS = 10.0


async def validate_discord_credentials(
    bot_token: str, *, client: httpx.AsyncClient | None = None
) -> tuple[bool, str | None, str | None]:
    """Returns (is_valid, external_account_id, error_message). external_account_id is the bot
    user's Discord snowflake id from the `id` field of GET /users/@me.

    `client` is injectable for tests (httpx.AsyncClient(transport=httpx.MockTransport(...))); it
    defaults to a real client when not provided.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await http_client.get(
                _DISCORD_ME_URL,
                headers={"Authorization": f"Bot {bot_token}"},
            )
        except httpx.HTTPError as exc:
            return False, None, f"could not reach Discord API: {exc}"
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        return False, None, f"Discord API rejected the credentials (HTTP {response.status_code})"

    body = response.json()
    external_account_id = str(body.get("id") or "")
    if not external_account_id:
        return False, None, "Discord API response did not include a bot user id"
    return True, external_account_id, None
