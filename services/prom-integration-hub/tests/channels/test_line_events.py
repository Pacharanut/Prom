"""Parsing raw LINE webhook JSON into LineEvent, and the dedup-key derivation."""

from __future__ import annotations

from src.channels.line.events import parse_line_webhook


def test_parses_text_message_event() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "message": {"type": "text", "id": "msg-1", "text": "สวัสดี"},
                "replyToken": "reply-1",
                "webhookEventId": "wh-1",
                "timestamp": 1000,
                "source": {"type": "user", "userId": "U123"},
            }
        ]
    }
    events = parse_line_webhook(payload)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "message"
    assert event.user_id == "U123"
    assert event.text == "สวัสดี"
    assert event.message_id == "msg-1"
    assert event.reply_token == "reply-1"


def test_parses_postback_event() -> None:
    payload = {
        "events": [
            {
                "type": "postback",
                "postback": {"data": "action=confirm"},
                "replyToken": "reply-2",
                "webhookEventId": "wh-2",
                "timestamp": 2000,
                "source": {"type": "user", "userId": "U456"},
            }
        ]
    }
    events = parse_line_webhook(payload)
    assert events[0].event_type == "postback"
    assert events[0].postback_data == "action=confirm"


def test_non_text_message_becomes_other() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "message": {"type": "image", "id": "img-1"},
                "source": {"type": "user", "userId": "U1"},
                "timestamp": 1,
            }
        ]
    }
    events = parse_line_webhook(payload)
    assert events[0].event_type == "other"


def test_dedup_key_prefers_webhook_event_id() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "message": {"type": "text", "id": "m1", "text": "hi"},
                "webhookEventId": "wh-abc",
                "timestamp": 1,
                "source": {"type": "user", "userId": "U1"},
            }
        ]
    }
    event = parse_line_webhook(payload)[0]
    assert event.dedup_key() == "line:wh-abc"


def test_dedup_key_falls_back_when_webhook_event_id_absent() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "message": {"type": "text", "id": "m1", "text": "hi"},
                "timestamp": 12345,
                "source": {"type": "user", "userId": "U1"},
            }
        ]
    }
    event = parse_line_webhook(payload)[0]
    assert event.webhook_event_id is None
    assert event.dedup_key() == "line:U1:m1:12345"


def test_same_event_delivered_twice_has_identical_dedup_key() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "message": {"type": "text", "id": "m1", "text": "hi"},
                "webhookEventId": "wh-dup",
                "timestamp": 1,
                "source": {"type": "user", "userId": "U1"},
            }
        ]
    }
    first = parse_line_webhook(payload)[0]
    second = parse_line_webhook(payload)[0]
    assert first.dedup_key() == second.dedup_key()
