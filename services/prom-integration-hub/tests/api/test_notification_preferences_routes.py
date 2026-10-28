"""HTTP-level tests for /api/v1/identities/{id}/notification-preferences (Phase 2B)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_get_notification_preferences,
    get_update_notification_preferences,
)
from apps.api.main import create_app
from src.application.notifications.preferences import (
    GetNotificationPreferences,
    UpdateNotificationPreferences,
)
from src.domain.identities.entities import IdentityStatus, LinkedIdentity
from src.domain.integrations.entities import ChannelType
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeLinkedIdentityRepository,
    FakeNotificationPreferenceRepository,
)
from tests.fakes.support import FakeClock

_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_NOW = datetime(2026, 10, 26, tzinfo=UTC)


async def _client() -> tuple[TestClient, LinkedIdentity]:
    identities = FakeLinkedIdentityRepository()
    identity = LinkedIdentity(
        id=uuid4(),
        customer_id=uuid4(),
        channel_installation_id=uuid4(),
        channel_type=ChannelType.LINE,
        channel_user_id="U1",
        prom_instance_id=uuid4(),
        prom_user_id="u1",
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=_NOW,
        unlinked_at=None,
    )
    await identities.add(identity)
    prefs = FakeNotificationPreferenceRepository()
    app = create_app()
    app.dependency_overrides[get_get_notification_preferences] = lambda: GetNotificationPreferences(
        linked_identities=identities, notification_preferences=prefs
    )
    app.dependency_overrides[get_update_notification_preferences] = lambda: (
        UpdateNotificationPreferences(
            linked_identities=identities,
            notification_preferences=prefs,
            audit_log=FakeAuditLogRepository(),
            clock=FakeClock(_NOW),
        )
    )
    return TestClient(app), identity


@pytest.mark.asyncio
async def test_get_then_put_preferences() -> None:
    client, identity = await _client()
    url = f"/api/v1/identities/{identity.id}/notification-preferences"

    first = client.get(url, headers=_HEADERS)
    assert first.status_code == 200
    body = first.json()
    assert body["preferences"][0] == {"event_type": "daily_summary", "enabled": True}
    assert len(body["preferences"]) == 9

    updated = client.put(
        url, headers=_HEADERS, json={"preferences": {"overdue": False, "comment": False}}
    )
    assert updated.status_code == 200
    disabled = {p["event_type"] for p in updated.json()["preferences"] if not p["enabled"]}
    assert disabled == {"overdue", "comment"}


@pytest.mark.asyncio
async def test_unknown_event_type_is_rejected() -> None:
    client, identity = await _client()
    response = client.put(
        f"/api/v1/identities/{identity.id}/notification-preferences",
        headers=_HEADERS,
        json={"preferences": {"everything": False}},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_identity_is_404_and_token_required() -> None:
    client, _ = await _client()
    url = f"/api/v1/identities/{uuid4()}/notification-preferences"
    assert client.get(url, headers=_HEADERS).status_code == 404
    assert client.get(url).status_code == 401
