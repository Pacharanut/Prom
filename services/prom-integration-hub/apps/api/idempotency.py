"""Explicit idempotency-check helper for Hub's own write endpoints (opt-in via the
`Idempotency-Key` header). Deliberately NOT a FastAPI dependency: a dependency alone can't short-
circuit route execution and substitute a cached response body, so each write route in
apps/api/routers/work_items.py calls `check_idempotency` at the top and `store_idempotency` after
computing the real response, rather than fighting FastAPI's DI model for something that needs to
intercept mid-handler.

See src/application/work_items/use_cases.py:CreateWorkItem's docstring for why this exists
alongside (not instead of) Prom's own external_id/external_source dedup on work-item creation —
they cover different failure modes and both are load-bearing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from src.application.work_items.ports import IdempotencyRepository
from src.domain.idempotency.entities import IdempotencyRecord
from src.shared.errors import IdempotencyKeyConflictError

_TTL = timedelta(hours=24)


def _hash_body(body: dict[str, Any]) -> str:
    normalized = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def check_idempotency(
    *,
    repository: IdempotencyRepository,
    customer_id: UUID,
    route: str,
    idempotency_key: str | None,
    request_body: dict[str, Any],
) -> dict[str, Any] | None:
    """Returns the cached response body if this exact request was already processed under this
    key, or None if the caller should proceed normally (no key given, or no prior record). Raises
    IdempotencyKeyConflictError if the key was reused with a different request body."""
    if idempotency_key is None:
        return None
    record = await repository.get(customer_id, route, idempotency_key)
    if record is None:
        return None
    if record.request_hash != _hash_body(request_body):
        raise IdempotencyKeyConflictError(
            f"Idempotency-Key {idempotency_key!r} was already used on {route!r} with a "
            "different request body"
        )
    result: dict[str, Any] = json.loads(record.response_body)
    return result


async def store_idempotency(
    *,
    repository: IdempotencyRepository,
    customer_id: UUID,
    route: str,
    idempotency_key: str | None,
    request_body: dict[str, Any],
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    if idempotency_key is None:
        return
    now = datetime.now(UTC)
    await repository.add(
        IdempotencyRecord(
            id=uuid4(),
            customer_id=customer_id,
            route=route,
            idempotency_key=idempotency_key,
            request_hash=_hash_body(request_body),
            response_status=response_status,
            response_body=json.dumps(response_body, default=str),
            created_at=now,
            expires_at=now + _TTL,
        )
    )
