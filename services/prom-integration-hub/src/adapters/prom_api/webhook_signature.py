"""Prom (Plane) inbound webhook signature verification. HMAC-SHA256 **hex digest** over the raw
request body, compared against the `X-Plane-Signature` header — verified against Plane's own
`webhook_send_task` (`apps/api/plane/bgtasks/webhook_task.py:296-304`, Gate 7 discovery this
session): `hmac.new(secret, json.dumps(payload).encode(), hashlib.sha256).hexdigest()`.

MUST fail closed, same principle as `src/channels/line/signature.py`: a missing/empty
`webhook_secret_encrypted` on the WorkspaceConnection (not yet configured — see that field's
docstring) is a reason to reject, not "skip verification."

Verifies against the **raw bytes actually received**, not a re-serialization of the parsed JSON —
re-serializing risks a key-order/whitespace mismatch against whatever Plane's `json.dumps` byte-
for-byte produced.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_prom_webhook_signature(
    raw_body: bytes, signature_header: str | None, secret: str | None
) -> bool:
    """Returns True only if `secret` is non-empty AND `signature_header` is present AND they
    match. Any missing input is an immediate False — never "allow through"."""
    if not secret or not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
