"""A minimal, framework-adjacent representation of one inbound Discord occurrence — the Discord
counterpart to src/channels/line/events.py's LineEvent. Built by
src/channels/discord/gateway_client.py from a real discord.py Message/Interaction (touching only
the handful of attributes it needs), so DiscordWebhookHandler itself never touches discord.py
types and stays trivially testable with plain dataclass instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class DiscordEvent:
    kind: Literal["message", "component", "other"]
    user_id: str
    text: str | None
    """DM message content — only set for kind="message"."""
    component_custom_id: str | None
    """The custom_id of the Button/Select that fired — only set for kind="component"."""
    component_value: str | None
    """For a Select interaction, the chosen option's value (interaction.data["values"][0]).
    Buttons carry no separate value — the custom_id alone identifies the action."""
    dedup_id: str
    """message.id or interaction.id, stringified — see dedup_key()."""
    group_id: str | None = None
    """Phase 2D: the text channel id when the event came from a server channel, not a DM."""

    def dedup_key(self) -> str:
        return f"discord:{self.kind}:{self.dedup_id}"
