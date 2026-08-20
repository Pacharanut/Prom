"""verify_prom_webhook_signature — fail-closed HMAC verification for inbound Prom (Plane)
webhooks. Same class of test as tests/channels/test_line_signature.py: the fail-closed case is
the single most important one here, since the equivalent bug (fail-open when the secret is
empty) is a documented real-world finding from Plane_Line during Gate 0 discovery.
"""

from __future__ import annotations

import hashlib
import hmac

from src.adapters.prom_api.webhook_signature import verify_prom_webhook_signature

_SECRET = "plane-webhook-secret"
_BODY = b'{"event":"issue","action":"update","data":{"id":"wi-1"}}'


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted() -> None:
    signature = _sign(_BODY, _SECRET)
    assert verify_prom_webhook_signature(_BODY, signature, _SECRET) is True


def test_invalid_signature_is_rejected() -> None:
    wrong_signature = _sign(_BODY, "a-different-secret")
    assert verify_prom_webhook_signature(_BODY, wrong_signature, _SECRET) is False


def test_tampered_body_is_rejected() -> None:
    signature = _sign(_BODY, _SECRET)
    tampered = _BODY.replace(b"update", b"delete")
    assert verify_prom_webhook_signature(tampered, signature, _SECRET) is False


def test_missing_secret_fails_closed_even_with_a_correctly_shaped_signature() -> None:
    """The exact bug found in Plane_Line during Gate 0 discovery
    (apps/api/plane/app/views/line/base.py:40-47): an unconfigured secret must reject, never
    "skip verification and allow through" — even if the caller supplies a signature that would
    validate against some other secret."""
    signature = _sign(_BODY, _SECRET)
    assert verify_prom_webhook_signature(_BODY, signature, None) is False
    assert verify_prom_webhook_signature(_BODY, signature, "") is False


def test_missing_signature_header_fails_closed() -> None:
    assert verify_prom_webhook_signature(_BODY, None, _SECRET) is False


def test_missing_both_fails_closed() -> None:
    assert verify_prom_webhook_signature(_BODY, None, None) is False
