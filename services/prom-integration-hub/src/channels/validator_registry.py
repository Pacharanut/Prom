"""Implements the application's ChannelCredentialValidator port by dispatching to the LINE or
Discord validator based on channel_type. Pure routing — no business logic of its own.
"""

from __future__ import annotations

from src.application.account_linking.ports import ChannelCredentialValidator, ValidationResult
from src.channels.discord.validator import validate_discord_credentials
from src.channels.line.validator import validate_line_credentials
from src.domain.integrations.entities import ChannelType


class ChannelValidatorRegistry(ChannelCredentialValidator):
    async def validate(
        self, channel_type: ChannelType, credentials: dict[str, str]
    ) -> ValidationResult:
        if channel_type is ChannelType.LINE:
            is_valid, external_account_id, error_message = await validate_line_credentials(
                credentials.get("channel_access_token", "")
            )
        elif channel_type is ChannelType.DISCORD:
            is_valid, external_account_id, error_message = await validate_discord_credentials(
                credentials.get("bot_token", "")
            )
        else:
            is_valid, external_account_id, error_message = False, None, "unsupported channel type"

        return ValidationResult(
            is_valid=is_valid,
            external_account_id=external_account_id,
            error_message=error_message,
        )
