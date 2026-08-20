"""Encode/decode LINE postback `data` strings. A flat query-string is simplest to build and parse
correctly (LINE's own 300-char limit on `data` is generous for this), and keeps every postback
action self-describing without needing a lookup table."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode


def encode_postback(action: str, **fields: str) -> str:
    return urlencode({"action": action, **fields})


def decode_postback(data: str) -> dict[str, str]:
    return dict(parse_qsl(data))
