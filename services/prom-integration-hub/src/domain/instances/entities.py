"""PromInstance — one deployed Plane/Prom Core instance belonging to a customer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse
from uuid import UUID

_LOCALHOST_LOOKING_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


class InstanceStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class InvalidPromInstanceUrlError(ValueError):
    """Raised when a PromInstance.base_url fails the https/non-local invariant check."""


@dataclass
class PromInstance:
    id: UUID
    customer_id: UUID
    name: str
    base_url: str
    status: InstanceStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_base_url(self.base_url)


def _validate_base_url(base_url: str) -> None:
    """Reject non-HTTPS or obviously-local URLs. A simple string/hostname check, mirroring the
    same-shaped validator Plane itself uses for outbound webhook URLs (see Gate 0 discovery) — no
    DNS resolution is performed, this is a defense-in-depth sanity check, not SSRF protection.
    """
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise InvalidPromInstanceUrlError(f"base_url must use https://, got: {base_url!r}")
    hostname = (parsed.hostname or "").lower()
    if hostname in _LOCALHOST_LOOKING_HOSTS or hostname.endswith(".local"):
        raise InvalidPromInstanceUrlError(f"base_url must not point at a local host: {base_url!r}")
