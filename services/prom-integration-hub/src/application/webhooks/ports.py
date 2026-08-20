"""Ports shared by every inbound-webhook channel controller (LINE, Discord) and, since Gate 7,
the inbound Prom webhook too. Framework-free — stdlib typing only.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from src.domain.webhooks.entities import WebhookEventRecord


class WebhookDedupStore(Protocol):
    async def mark_seen_if_new(self, event_key: str, ttl_seconds: int) -> bool:
        """Atomically check-and-record one webhook event id. Returns True the first time a given
        `event_key` is seen (caller should process the event), False on every subsequent call
        with the same key within `ttl_seconds` (caller should skip it — LINE/Discord redeliver
        webhooks, and Hub must not create/send duplicates as a result; see the master prompt's
        webhook §11 "บันทึก event ID และป้องกัน replay" requirement and DoD §20 "Duplicate
        webhook/message ไม่สร้างหรือส่งซ้ำ").

        Gate 7's Prom webhook uses `WebhookEventRepository` (below) instead of this store — a
        durable Postgres row, not a Redis TTL entry, because the inbound Prom event also needs a
        processed/failed/dead_letter audit trail for retry bookkeeping, not just dedup. LINE/
        Discord only need the dedup bit itself (no async retry pipeline sits behind them), so
        Redis remains the right tool there."""
        ...


class WebhookEventRepository(Protocol):
    async def get_by_delivery_id(self, delivery_id: str) -> WebhookEventRecord | None: ...
    async def get(self, record_id: UUID) -> WebhookEventRecord | None: ...
    async def add(self, record: WebhookEventRecord) -> WebhookEventRecord | None:
        """Persist a new record. Returns None (not the inserted record) if a concurrent request
        already inserted the same `delivery_id` first (a real, reachable race — Plane can retry a
        delivery before the first attempt's DB write completes) — the caller should treat that as
        "already being processed, skip" exactly like a normal dedup hit, not an error. See
        PostgresWebhookEventRepository.add for the SAVEPOINT translation."""
        ...

    async def save(self, record: WebhookEventRecord) -> None: ...
