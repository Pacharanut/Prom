"""LINE webhook signature verification — the fail-closed behavior is the single most important
test in this gate (see the real bug found in Plane_Line during Gate 0 discovery: fail-open on an
empty secret)."""

from __future__ import annotations

import base64
import hashlib
import hmac

from src.channels.line.signature import verify_line_signature

_SECRET = "channel-secret-123"
_BODY = b'{"events":[]}'


def _sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_valid_signature_is_accepted() -> None:
    signature = _sign(_SECRET, _BODY)
    assert verify_line_signature(_BODY, signature, _SECRET) is True


def test_invalid_signature_is_rejected() -> None:
    assert verify_line_signature(_BODY, "not-the-right-signature==", _SECRET) is False


def test_missing_secret_fails_closed_even_with_a_signature_header() -> None:
    """The critical regression test: an empty/unset channel_secret must REJECT, never "skip
    verification and allow through" (the exact bug found in Plane_Line)."""
    signature = _sign(_SECRET, _BODY)
    assert verify_line_signature(_BODY, signature, "") is False
    assert verify_line_signature(_BODY, signature, None) is False


def test_missing_signature_header_fails_closed() -> None:
    assert verify_line_signature(_BODY, None, _SECRET) is False


def test_both_missing_fails_closed() -> None:
    assert verify_line_signature(_BODY, None, None) is False
    assert verify_line_signature(_BODY, "", "") is False


def test_signature_for_different_body_is_rejected() -> None:
    signature = _sign(_SECRET, _BODY)
    assert verify_line_signature(b'{"events":["tampered"]}', signature, _SECRET) is False
