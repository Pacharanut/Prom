"""HTTP-level tests for the public Prom (Plane) inbound webhook route: signature verification
wired to the real X-Plane-Signature header, workspace-connection lookup, X-Plane-Delivery dedup,
and enqueueing the processing task. Application-layer notification routing is already covered by
tests/application/test_notifications.py — this file only proves the route's own plumbing (auth,
404s, dedup, task enqueueing), same division of concerns as tests/api/test_line_webhook_routes.py.

`celery_app.send_task` is monkeypatched rather than requiring a live Redis broker — this is the
infra-free unit tier; the live-broker path is exercised manually/in the integration tier's Docker
Compose stack, not here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_secret_provider,
    get_webhook_event_repository,
    get_workspace_connection_repository,
)
from apps.api.main import create_app
from apps.api.routers import prom_webhook
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.workspaces.entities import WorkspaceConnection
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import FakeWebhookEventRepository, FakeWorkspaceConnectionRepository

_SECRET = "plane-webhook-secret"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _payload(*, delivery: str = "delivery-1") -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "event": "issue",
            "action": "created",
            "webhook_id": "wh-1",
            "workspace_id": "ws-1",
            "workspace_slug": "acme",
            "data": {"id": "wi-1", "assignees": []},
            "activity": None,
        }
    ).encode()
    headers = {
        "X-Plane-Signature": _sign(body),
        "X-Plane-Delivery": delivery,
        "X-Plane-Event": "issue",
        "Content-Type": "application/json",
    }
    return body, headers


async def _seed_workspace(secret: str | None = _SECRET) -> tuple[WorkspaceConnection, object]:
    secrets_provider = FernetSecretProvider(get_settings().secret_encryption_key)
    now = _NOW
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=now, updated_at=now
    )
    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="Acme Prom",
        base_url="https://prom.acme.example",
        status=InstanceStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    workspace = WorkspaceConnection(
        id=uuid4(),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        prom_workspace_id="ws-1",
        prom_workspace_slug="acme",
        name="Acme Workspace",
        created_at=now,
        updated_at=now,
        webhook_secret_encrypted=(
            secrets_provider.encrypt(secret.encode("utf-8")) if secret is not None else None
        ),
    )
    workspace_connections = FakeWorkspaceConnectionRepository()
    await workspace_connections.add(workspace)
    return workspace, workspace_connections


def _build_client(
    workspace_connections, webhook_events, *, sent_tasks: list, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_workspace_connection_repository] = lambda: workspace_connections
    app.dependency_overrides[get_webhook_event_repository] = lambda: webhook_events
    app.dependency_overrides[get_secret_provider] = lambda: FernetSecretProvider(
        get_settings().secret_encryption_key
    )

    def _fake_send_task(name: str, kwargs: dict) -> None:
        sent_tasks.append((name, kwargs))

    # monkeypatch (not a raw assignment) so this is reverted after the test even if it fails —
    # celery_app is a process-wide singleton shared with every other test module that imports it.
    monkeypatch.setattr(prom_webhook.celery_app, "send_task", _fake_send_task)
    return TestClient(app)


@pytest.mark.asyncio
async def test_valid_signature_persists_record_and_enqueues_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, workspace_connections = await _seed_workspace()
    webhook_events = FakeWebhookEventRepository()
    sent_tasks: list = []
    client = _build_client(
        workspace_connections, webhook_events, sent_tasks=sent_tasks, monkeypatch=monkeypatch
    )
    body, headers = _payload()

    response = client.post(f"/webhooks/v1/prom/{workspace.id}", content=body, headers=headers)

    assert response.status_code == 200
    assert len(sent_tasks) == 1
    task_name, kwargs = sent_tasks[0]
    assert task_name == "notifications.process_prom_webhook_event"
    assert kwargs["workspace_connection_id"] == str(workspace.id)
    record = await webhook_events.get_by_delivery_id("delivery-1")
    assert record is not None
    assert record.event == "issue"
    assert record.action == "created"


@pytest.mark.asyncio
async def test_duplicate_delivery_id_acks_without_reenqueueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, workspace_connections = await _seed_workspace()
    webhook_events = FakeWebhookEventRepository()
    sent_tasks: list = []
    client = _build_client(
        workspace_connections, webhook_events, sent_tasks=sent_tasks, monkeypatch=monkeypatch
    )
    body, headers = _payload(delivery="delivery-dup")

    first = client.post(f"/webhooks/v1/prom/{workspace.id}", content=body, headers=headers)
    second = client.post(f"/webhooks/v1/prom/{workspace.id}", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(sent_tasks) == 1  # not enqueued twice


@pytest.mark.asyncio
async def test_unconfigured_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, workspace_connections = await _seed_workspace(secret=None)
    webhook_events = FakeWebhookEventRepository()
    sent_tasks: list = []
    client = _build_client(
        workspace_connections, webhook_events, sent_tasks=sent_tasks, monkeypatch=monkeypatch
    )
    body, headers = _payload()

    response = client.post(f"/webhooks/v1/prom/{workspace.id}", content=body, headers=headers)

    assert response.status_code == 401
    assert sent_tasks == []


@pytest.mark.asyncio
async def test_wrong_signature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, workspace_connections = await _seed_workspace()
    webhook_events = FakeWebhookEventRepository()
    sent_tasks: list = []
    client = _build_client(
        workspace_connections, webhook_events, sent_tasks=sent_tasks, monkeypatch=monkeypatch
    )
    body, _headers = _payload()

    response = client.post(
        f"/webhooks/v1/prom/{workspace.id}",
        content=body,
        headers={"X-Plane-Signature": "wrong", "X-Plane-Delivery": "delivery-1"},
    )

    assert response.status_code == 401
    assert sent_tasks == []


def test_unknown_workspace_connection_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_connections = FakeWorkspaceConnectionRepository()
    webhook_events = FakeWebhookEventRepository()
    client = _build_client(
        workspace_connections, webhook_events, sent_tasks=[], monkeypatch=monkeypatch
    )
    body, headers = _payload()

    response = client.post(f"/webhooks/v1/prom/{uuid4()}", content=body, headers=headers)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_missing_delivery_id_on_signed_request_is_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, workspace_connections = await _seed_workspace()
    webhook_events = FakeWebhookEventRepository()
    sent_tasks: list = []
    client = _build_client(
        workspace_connections, webhook_events, sent_tasks=sent_tasks, monkeypatch=monkeypatch
    )
    body, headers = _payload()
    del headers["X-Plane-Delivery"]

    response = client.post(f"/webhooks/v1/prom/{workspace.id}", content=body, headers=headers)

    assert response.status_code == 400
    assert sent_tasks == []
