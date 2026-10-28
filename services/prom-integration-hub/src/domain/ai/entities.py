"""AiSettings — per-customer configuration of the LLM gateway the AI assistant calls (Phase 2,
see docs/architecture/04-adr-001-ai-intent-layer.md). Managed from Plane's
Settings → AI Assistant page through prom_bridge; when no row exists for a customer, the Hub's
own environment defaults (NT_AI_HUB_*) apply instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID

MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 60


class InvalidAiSettingsError(ValueError):
    """Raised when AiSettings is constructed with an unusable base_url/model/timeout."""


@dataclass
class AiSettings:
    id: UUID
    customer_id: UUID
    is_enabled: bool
    base_url: str
    """OpenAI-compatible API base, e.g. https://aihub.ntaisolution.com/core-api/v1 — the path
    that `/chat/completions` and `/models` are appended to."""
    encrypted_api_key: bytes | None
    """None means "use the Hub's NT_AI_HUB_KEY environment default", not "no key"."""
    model: str
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        validate_ai_connection_fields(self.base_url, self.model, self.timeout_seconds)


def validate_ai_connection_fields(base_url: str, model: str, timeout_seconds: int) -> None:
    """http:// is allowed (unlike PromInstance.base_url): a self-hosted gateway on a private
    network commonly has no TLS, and the admin setting it is already trusted with the key."""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidAiSettingsError(f"base_url must be an http(s) URL, got: {base_url!r}")
    if not model.strip():
        raise InvalidAiSettingsError("model must not be empty")
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise InvalidAiSettingsError(
            f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
        )
