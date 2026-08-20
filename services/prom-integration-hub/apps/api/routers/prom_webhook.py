"""Public inbound Prom (Plane) webhook endpoint — NOT behind require_service_token (Plane calls
this directly; its own auth is the HMAC signature check below, which fails closed on any
missing/invalid input, same pattern as apps/api/routers/line_webhook.py).

Fast-ack, heavy work queued: this route only verifies the signature, checks the `X-Plane-Delivery`
header against WebhookEventRepository for a duplicate, persists a WebhookEventRecord, and enqueues
a Celery task — it does not itself decide who to notify or send anything. See
src/application/notifications/use_cases.py's ProcessPromWebhookEvent docstring and
src/infrastructure/messaging/notification_tasks.py for why "send heavy work to queue" applies here
but deliberately not to LINE/Discord's synchronous chat replies (a different transport with a
different UX contract — a user waiting on a chat reply cannot be queued, but Plane never waits on
a webhook "reply" the way a chat user waits on a message).
"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response

from apps.api.dependencies import (
    get_secret_provider,
    get_webhook_event_repository,
    get_workspace_connection_repository,
)
from src.adapters.clock import SystemClock
from src.adapters.persistence.webhook_event_repository import PostgresWebhookEventRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.adapters.prom_api.webhook_signature import verify_prom_webhook_signature
from src.domain.webhooks.entities import WebhookEventRecord, WebhookEventStatus
from src.infrastructure.messaging.celery_app import celery_app

router = APIRouter(prefix="/webhooks/v1/prom", tags=["prom-webhook"])


@router.post("/{workspace_connection_id}", status_code=200)
async def receive_prom_webhook(
    workspace_connection_id: UUID,
    request: Request,
    workspace_connections: Annotated[
        PostgresWorkspaceConnectionRepository, Depends(get_workspace_connection_repository)
    ],
    webhook_events: Annotated[
        PostgresWebhookEventRepository, Depends(get_webhook_event_repository)
    ],
) -> Response:
    x_plane_signature = request.headers.get("x-plane-signature")
    x_plane_delivery = request.headers.get("x-plane-delivery")

    workspace = await workspace_connections.get(workspace_connection_id)
    if workspace is None:
        # Deliberately vague (404, no detail) — do not reveal whether a workspace_connection_id
        # exists to an unauthenticated caller, same principle as the LINE webhook route.
        return Response(status_code=404)

    secret: str | None = None
    if workspace.webhook_secret_encrypted is not None:
        secret = get_secret_provider().decrypt(workspace.webhook_secret_encrypted).decode("utf-8")

    raw_body = await request.body()
    if not verify_prom_webhook_signature(raw_body, x_plane_signature, secret):
        # Fail closed: unconfigured secret, missing header, or a mismatch are all rejected
        # identically — see src/adapters/prom_api/webhook_signature.py's module docstring.
        return Response(status_code=401)

    if not x_plane_delivery:
        # Plane always sends this (apps/api/plane/bgtasks/webhook_task.py:270, verified this
        # session) — a signed request without it is malformed, not a replay to dedup, reject.
        return Response(status_code=400)

    existing = await webhook_events.get_by_delivery_id(x_plane_delivery)
    if existing is not None:
        # Already recorded (a genuine Plane retry, or a race the repository's own SAVEPOINT-
        # wrapped add() will also catch) — ack without reprocessing, per §11/§20's duplicate-
        # webhook requirements.
        return Response(status_code=200)

    payload = json.loads(raw_body)
    now = SystemClock().now()
    record = WebhookEventRecord(
        id=uuid4(),
        workspace_connection_id=workspace_connection_id,
        delivery_id=x_plane_delivery,
        event=str(payload.get("event", "")),
        action=str(payload.get("action", "")),
        received_at=now,
        processed_at=None,
        status=WebhookEventStatus.RECEIVED,
    )
    stored = await webhook_events.add(record)
    if stored is None:
        # Lost a concurrent-insert race against the same delivery_id (see
        # PostgresWebhookEventRepository.add's docstring) — the other request is already handling
        # it, ack without enqueueing a second processing task.
        return Response(status_code=200)

    celery_app.send_task(
        "notifications.process_prom_webhook_event",
        kwargs={
            "workspace_connection_id": str(workspace_connection_id),
            "webhook_event_record_id": str(stored.id),
            "payload": payload,
        },
    )
    return Response(status_code=200)
