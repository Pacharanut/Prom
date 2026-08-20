"""HTTP-level test for the public LINE webhook route: signature verification wired to the real
X-Line-Signature header, installation lookup, and dispatch into LineWebhookHandler. Full
conversation-flow behavior is already covered by tests/channels/test_line_handler.py — this file
only proves the route's own plumbing (auth, 404s, request wiring), same division of concerns as
tests/api/test_work_items_routes.py vs tests/application/test_work_items.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import get_channel_installation_repository, get_line_webhook_handler
from apps.api.main import create_app
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.application.account_linking.use_cases import CreateChannelInstallation
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.integrations.entities import ChannelType
from src.infrastructure.configuration.settings import get_settings
from tests.channels.conftest import LineHandlerHarness
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeChannelInstallationRepository,
    FakeCustomerRepository,
    FakeDelegatedCredentialRepository,
    FakeLinkedIdentityRepository,
    FakeLinkingCodeRepository,
    FakePromInstanceRepository,
    FakeWorkspaceConnectionRepository,
)
from tests.fakes.support import (
    FakeChannelCredentialValidator,
    FakeClock,
    FakeConversationStateRepository,
    FakePromApiClient,
    FakeRateLimiter,
    FakeWebhookDedupStore,
)

_CHANNEL_SECRET = "webhook-secret"


def _sign(body: bytes) -> str:
    return base64.b64encode(
        hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()


def _new_harness() -> LineHandlerHarness:
    """Uses the REAL FernetSecretProvider (keyed from settings, same as the webhook route's own
    `get_secret_provider()`) rather than FakeSecretProvider — this test exercises the actual
    route, which decrypts installation credentials via `get_secret_provider()` outside of any
    dependency-overridable path, so the harness that seeds those credentials must encrypt them
    the same way or decryption would fail against mismatched "encryption"."""
    return LineHandlerHarness(
        customers=FakeCustomerRepository(),
        prom_instances=FakePromInstanceRepository(),
        workspace_connections=FakeWorkspaceConnectionRepository(),
        installations=FakeChannelInstallationRepository(),
        linked_identities=FakeLinkedIdentityRepository(),
        linking_codes=FakeLinkingCodeRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
        secrets_provider=FernetSecretProvider(get_settings().secret_encryption_key),
        prom_api_client=FakePromApiClient(),
        rate_limiter=FakeRateLimiter(),
        dedup_store=FakeWebhookDedupStore(),
        conversation_states=FakeConversationStateRepository(),
    )


async def _seed() -> tuple[LineHandlerHarness, str]:
    harness = _new_harness()
    customer = Customer(
        id=uuid4(),
        name="Acme",
        status=CustomerStatus.ACTIVE,
        created_at=harness.clock.now(),
        updated_at=harness.clock.now(),
    )
    await harness.customers.add(customer)

    installation = await CreateChannelInstallation(
        installations=harness.installations,
        validator=FakeChannelCredentialValidator(),
        secrets_provider=harness.secrets_provider,
        audit_log=harness.audit_log,
        clock=harness.clock,
    ).execute(
        customer_id=customer.id,
        channel_type=ChannelType.LINE,
        display_name="Acme LINE Bot",
        credentials={"channel_access_token": "token-1", "channel_secret": _CHANNEL_SECRET},
        correlation_id="seed",
    )
    return harness, str(installation.id)


def _build_client(harness: LineHandlerHarness) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_channel_installation_repository] = lambda: harness.installations
    app.dependency_overrides[get_line_webhook_handler] = lambda: harness.handler
    return TestClient(app)


@pytest.mark.asyncio
async def test_valid_signature_is_accepted_and_dispatched() -> None:
    harness, installation_id = await _seed()
    client = _build_client(harness)
    body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "message": {"type": "text", "id": "m1", "text": "เมนู"},
                    "replyToken": "reply-1",
                    "webhookEventId": "wh-1",
                    "timestamp": 1,
                    "source": {"type": "user", "userId": "U1"},
                }
            ]
        }
    ).encode()

    response = client.post(
        f"/webhooks/v1/line/{installation_id}",
        content=body,
        headers={"X-Line-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_missing_signature_is_rejected() -> None:
    harness, installation_id = await _seed()
    client = _build_client(harness)
    body = b'{"events":[]}'

    response = client.post(
        f"/webhooks/v1/line/{installation_id}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_signature_is_rejected() -> None:
    harness, installation_id = await _seed()
    client = _build_client(harness)
    body = b'{"events":[]}'

    response = client.post(
        f"/webhooks/v1/line/{installation_id}",
        content=body,
        headers={"X-Line-Signature": "wrong==", "Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_unknown_installation_is_404() -> None:
    harness = _new_harness()
    client = _build_client(harness)
    response = client.post(f"/webhooks/v1/line/{uuid4()}", content=b'{"events":[]}')
    assert response.status_code == 404
