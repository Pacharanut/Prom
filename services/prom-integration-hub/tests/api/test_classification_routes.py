"""HTTP-level tests for /api/v1/workspace-connections/{id}/classification (Phase 2C)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_get_classification_settings,
    get_update_classification_settings,
)
from apps.api.main import create_app
from src.application.classification.settings import (
    GetClassificationSettings,
    UpdateClassificationSettings,
)
from src.domain.workspaces.entities import WorkspaceConnection
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeClassificationRepository,
    FakeWorkspaceConnectionRepository,
)
from tests.fakes.support import FakeClock

_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_NOW = datetime(2026, 10, 26, tzinfo=UTC)


async def _client() -> tuple[TestClient, WorkspaceConnection]:
    workspaces = FakeWorkspaceConnectionRepository()
    ws = WorkspaceConnection(
        id=uuid4(),
        customer_id=uuid4(),
        prom_instance_id=uuid4(),
        prom_workspace_id="w",
        prom_workspace_slug="acme",
        name="Acme",
        created_at=_NOW,
        updated_at=_NOW,
    )
    await workspaces.add(ws)
    repo = FakeClassificationRepository()
    app = create_app()
    app.dependency_overrides[get_get_classification_settings] = lambda: GetClassificationSettings(
        workspace_connections=workspaces, classification=repo
    )
    app.dependency_overrides[get_update_classification_settings] = lambda: (
        UpdateClassificationSettings(
            workspace_connections=workspaces,
            classification=repo,
            audit_log=FakeAuditLogRepository(),
            clock=FakeClock(_NOW),
        )
    )
    return TestClient(app), ws


@pytest.mark.asyncio
async def test_defaults_then_replace_rules() -> None:
    client, ws = await _client()
    url = f"/api/v1/workspace-connections/{ws.id}/classification"

    first = client.get(url, headers=_HEADERS).json()
    assert first == {
        "workspace_connection_id": str(ws.id),
        "is_enabled": True,
        "use_ai_fallback": True,
        "rules": [],
        "is_customized": False,
    }

    body = {
        "is_enabled": True,
        "use_ai_fallback": False,
        "rules": [
            {"keyword": "bug", "label_name": "Bug", "priority": "high"},
            {"keyword": "ด่วน", "priority": "urgent"},
        ],
    }
    saved = client.put(url, headers=_HEADERS, json=body)
    assert saved.status_code == 200
    assert saved.json()["rules"][1] == {"keyword": "ด่วน", "label_name": None, "priority": "urgent"}
    assert client.get(url, headers=_HEADERS).json()["is_customized"] is True


@pytest.mark.asyncio
async def test_invalid_rules_rejected_and_unknown_workspace_404() -> None:
    client, ws = await _client()
    url = f"/api/v1/workspace-connections/{ws.id}/classification"
    bad = client.put(url, headers=_HEADERS, json={"rules": [{"keyword": "x"}]})
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "PROM_REQUEST_INVALID"
    wrong_priority = client.put(
        url, headers=_HEADERS, json={"rules": [{"keyword": "x", "priority": "critical"}]}
    )
    assert wrong_priority.status_code == 422
    missing = client.get(
        f"/api/v1/workspace-connections/{uuid4()}/classification", headers=_HEADERS
    )
    assert missing.status_code == 404
