"""Parses a raw LINE webhook JSON payload into typed, minimal LineEvent objects. Only the
`message` (text) and `postback` event types are handled — anything else (follow/unfollow, image
messages, etc.) is parsed as `event_type="other"` and the handler ignores it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class LineEvent:
    event_type: Literal["message", "postback", "other"]
    user_id: str | None
    reply_token: str | None
    text: str | None
    postback_data: str | None
    message_id: str | None
    webhook_event_id: str | None
    timestamp: int | None

    def dedup_key(self) -> str:
        """Prefers LINE's own `webhookEventId` (present on recent API versions) — falls back to a
        stable composite of user/message/timestamp when it's absent, per the master prompt's
        webhook replay-protection requirement (§11)."""
        if self.webhook_event_id:
            return f"line:{self.webhook_event_id}"
        distinguishing = self.message_id or self.postback_data or ""
        return f"line:{self.user_id}:{distinguishing}:{self.timestamp}"


def parse_line_webhook(payload: dict[str, Any]) -> list[LineEvent]:
    events: list[LineEvent] = []
    for raw_event in payload.get("events", []):
        source = raw_event.get("source") or {}
        user_id = source.get("userId")
        reply_token = raw_event.get("replyToken")
        webhook_event_id = raw_event.get("webhookEventId")
        timestamp = raw_event.get("timestamp")
        raw_type = raw_event.get("type")

        if raw_type == "message" and (raw_event.get("message") or {}).get("type") == "text":
            message = raw_event["message"]
            events.append(
                LineEvent(
                    event_type="message",
                    user_id=user_id,
                    reply_token=reply_token,
                    text=message.get("text"),
                    postback_data=None,
                    message_id=message.get("id"),
                    webhook_event_id=webhook_event_id,
                    timestamp=timestamp,
                )
            )
        elif raw_type == "postback":
            postback = raw_event.get("postback") or {}
            events.append(
                LineEvent(
                    event_type="postback",
                    user_id=user_id,
                    reply_token=reply_token,
                    text=None,
                    postback_data=postback.get("data"),
                    message_id=None,
                    webhook_event_id=webhook_event_id,
                    timestamp=timestamp,
                )
            )
        else:
            events.append(
                LineEvent(
                    event_type="other",
                    user_id=user_id,
                    reply_token=reply_token,
                    text=None,
                    postback_data=None,
                    message_id=None,
                    webhook_event_id=webhook_event_id,
                    timestamp=timestamp,
                )
            )
    return events
