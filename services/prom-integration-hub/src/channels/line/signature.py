"""LINE webhook signature verification. HMAC-SHA256 over the raw request body, base64-encoded,
compared against the `X-Line-Signature` header.

MUST fail closed: if the channel secret is missing/empty, this is a misconfiguration, not "skip
verification" — the exact opposite of the bug found in `Plane_Line/apps/api/plane/app/views/line/
base.py:40-47` during Gate 0 discovery (that code returned True — verification passed — when the
secret was empty, which would accept forged/unsigned webhooks). Reference-only finding, not
copied code; this implementation is written fresh with the fix baked in from the start.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def verify_line_signature(
    raw_body: bytes, signature_header: str | None, channel_secret: str | None
) -> bool:
    """Returns True only if `channel_secret` is non-empty AND `signature_header` is present AND
    they match. Any missing input is an immediate False — never "allow through"."""
    if not channel_secret or not signature_header:
        return False
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature_header)
