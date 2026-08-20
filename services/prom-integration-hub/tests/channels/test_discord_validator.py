"""Discord credential validation, using httpx.MockTransport (no real network call)."""

from __future__ import annotations

import httpx
import pytest

from src.channels.discord.validator import validate_discord_credentials


@pytest.mark.asyncio
async def test_valid_token_returns_bot_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bot good-token"
        return httpx.Response(200, json={"id": "123456789", "username": "acme-bot"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        is_valid, external_account_id, error_message = await validate_discord_credentials(
            "good-token", client=client
        )

    assert is_valid is True
    assert external_account_id == "123456789"
    assert error_message is None


@pytest.mark.asyncio
async def test_invalid_token_returns_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "401: Unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        is_valid, external_account_id, error_message = await validate_discord_credentials(
            "bad-token", client=client
        )

    assert is_valid is False
    assert external_account_id is None
    assert error_message is not None and "401" in error_message
