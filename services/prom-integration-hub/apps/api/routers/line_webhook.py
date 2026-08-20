"""Public LINE webhook endpoint — NOT behind require_service_token (LINE calls this directly;
its own auth is the HMAC signature check below, which fails closed on any missing/invalid input).
"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from apps.api.dependencies import (
    get_channel_installation_repository,
    get_line_webhook_handler,
    get_secret_provider,
)
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.channels.line.handler import LineWebhookHandler
from src.channels.line.signature import verify_line_signature
from src.domain.integrations.entities import ChannelType, InstallationStatus

router = APIRouter(prefix="/webhooks/v1/line", tags=["line-webhook"])


@router.post("/{installation_id}", status_code=200)
async def receive_line_webhook(
    installation_id: UUID,
    request: Request,
    handler: Annotated[LineWebhookHandler, Depends(get_line_webhook_handler)],
    installations: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
) -> Response:
    x_line_signature = request.headers.get("x-line-signature")

    installation = await installations.get(installation_id)
    if installation is None or installation.channel_type is not ChannelType.LINE:
        # Deliberately vague (404, no detail) — do not reveal whether an installation_id exists
        # to an unauthenticated caller.
        return Response(status_code=404)
    if installation.status is not InstallationStatus.ACTIVE:
        return Response(status_code=404)

    credentials: dict[str, str] = json.loads(
        get_secret_provider().decrypt(installation.encrypted_credentials).decode("utf-8")
    )
    channel_secret = credentials.get("channel_secret")
    channel_access_token = credentials.get("channel_access_token", "")

    raw_body = await request.body()
    if not verify_line_signature(raw_body, x_line_signature, channel_secret):
        # Fail closed: missing/empty channel_secret, missing header, or a mismatch are all
        # rejected identically — see src/channels/line/signature.py's module docstring for why
        # this must never fall back to "allow through".
        return Response(status_code=401)

    payload = json.loads(raw_body)
    await handler.handle_payload(installation, channel_access_token, payload)
    return Response(status_code=200)
