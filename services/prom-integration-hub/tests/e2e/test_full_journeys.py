"""Gate 9 E2E tests: real HTTP calls through the actual FastAPI app (`create_app()`), real
Postgres via `db_session` (the same transaction-scoped fixture the integration tier already
uses), real dependency-injection wiring from `apps/api/dependencies.py` — not fakes standing in
for whole use cases the way `tests/api/*` does. Only things that must be external (LINE/Discord
credential validation, Prom's own REST API, LINE/Discord message delivery) are faked; everything
else — repositories, use cases, routers, idempotency, error mapping — is the real production
wiring, chained across multiple real requests, run against a live Postgres.

Run with: pytest -m integration (see README "Integration tests").
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_session
from apps.api.main import create_app
from apps.api.routers import prom_webhook
from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.delegated_credential_repository import (
    PostgresDelegatedCredentialRepository,
)
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.notification_preference_repository import (
    PostgresNotificationPreferenceRepository,
)
from src.adapters.persistence.outbound_message_repository import PostgresOutboundMessageRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.webhook_event_repository import PostgresWebhookEventRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.application.notifications.use_cases import FlushOutboundMessage, ProcessPromWebhookEvent
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemState
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.support import FakeChannelCredentialValidator, FakeClock, FakeMessagePublisher

pytestmark = pytest.mark.integration

_SERVICE_TOKEN_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}


def _client(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    *,
    validator: FakeChannelCredentialValidator | None = None,
    prom_api_client=None,
) -> httpx.AsyncClient:
    """Wires the real app against the test's real (rolled-back-on-teardown) Postgres session, and
    fakes only the two ports that would otherwise make real outbound HTTP calls to LINE/Discord/
    Plane. Every repository, every use case, every router is the real production object.

    Uses `httpx.AsyncClient` + `ASGITransport`, NOT the sync `starlette.testclient.TestClient` —
    the sync client runs the app in a separate thread with its own event loop, which breaks any
    override backed by a real asyncpg connection (loop-bound, per the exact "Future attached to a
    different loop" bug class already documented in tests/integration/conftest.py's db_session
    fixture). Every existing `tests/api/*` route test uses the sync TestClient safely only because
    it overrides with in-memory fakes, never a real DB connection — this file is the first to wire
    real Postgres through real HTTP, so it needs the async-native client to stay in the same loop.
    """
    # apps/api/dependencies.py's per-use-case factories (get_create_channel_installation,
    # get_create_work_item, etc.) call get_credential_validator()/get_prom_api_client() as plain
    # function calls inside their own body, NOT as FastAPI Depends(...) sub-dependencies -- so
    # app.dependency_overrides never sees or intercepts those inner calls. Monkeypatching the
    # module-level functions themselves (Python resolves the bare name at call time against the
    # module's globals) reaches every factory uniformly, without overriding each one individually.
    resolved_validator = validator or FakeChannelCredentialValidator()
    monkeypatch.setattr(
        "apps.api.dependencies.get_credential_validator", lambda: resolved_validator
    )
    # Always faked, never just when a caller passes one: account-linking's start step calls
    # prom_api_client.get_current_user() against instance.base_url (a fake "https://prom.acme
    # .example" seeded by _seed_customer_instance_workspace) to validate the delegated token —
    # every journey that starts account linking hits this, not only the work-item journey, so
    # leaving the real HttpPromApiClient wired in for the others caused a real outbound DNS
    # lookup and a 502 PROM_API_UNAVAILABLE.
    resolved_prom_api_client = prom_api_client or _seeded_fake_prom_api_client()
    monkeypatch.setattr(
        "apps.api.dependencies.get_prom_api_client", lambda: resolved_prom_api_client
    )
    # get_redis_client() is @lru_cache'd process-wide (src/adapters/redis/client.py) for the real
    # app's single long-lived event loop -- the exact same bug class as get_engine() documented in
    # tests/integration/conftest.py's db_session fixture. pytest-asyncio gives each test its own
    # event loop, so once any test binds the cached client to its loop, the next test's use of it
    # (e.g. ConsumeLinkingCode's rate limiter) raises "Event loop is closed"/cross-loop Future
    # errors. Monkeypatching to the per-test `redis_client` fixture (own connection, flushed and
    # closed on teardown) avoids the cross-test sharing entirely.
    monkeypatch.setattr("apps.api.dependencies.get_redis_client", lambda: redis_client)

    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _seed_customer_instance_workspace(client: httpx.AsyncClient) -> tuple[str, str, str]:
    customer = await client.post(
        "/api/v1/customers", headers=_SERVICE_TOKEN_HEADERS, json={"name": "Acme Corp"}
    )
    assert customer.status_code == 201, customer.text
    customer_id = customer.json()["id"]

    instance = await client.post(
        "/api/v1/prom-instances",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": customer_id,
            "name": "Acme Prom",
            "base_url": "https://prom.acme.example",
        },
    )
    assert instance.status_code == 201, instance.text
    instance_id = instance.json()["id"]

    workspace = await client.post(
        "/api/v1/workspace-connections",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": customer_id,
            "prom_instance_id": instance_id,
            "prom_workspace_id": "ws-1",
            "prom_workspace_slug": "acme",
            "name": "Acme Workspace",
        },
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    return customer_id, instance_id, workspace_id


@pytest.mark.asyncio
async def test_full_account_linking_journey_through_real_http_and_postgres(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    client = _client(db_session, monkeypatch, redis_client)
    customer_id, instance_id, workspace_id = await _seed_customer_instance_workspace(client)

    installation = await client.post(
        "/api/v1/installations",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": customer_id,
            "channel_type": "line",
            "display_name": "Acme LINE Bot",
            "credentials": {"channel_access_token": "token-123", "channel_secret": "secret-123"},
        },
    )
    assert installation.status_code == 201, installation.text
    installation_id = installation.json()["id"]

    link = await client.post(
        f"/api/v1/installations/{installation_id}/workspaces",
        headers=_SERVICE_TOKEN_HEADERS,
        json={"workspace_connection_id": workspace_id, "is_default": True},
    )
    assert link.status_code == 201, link.text

    start = await client.post(
        "/api/v1/account-linking/start",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "prom_instance_id": instance_id,
            "prom_user_id": "prom-user-1",
            "channel_installation_id": installation_id,
            "delegated_token": "delegated-token",
            "default_workspace_connection_id": workspace_id,
        },
    )
    assert start.status_code == 201, start.text
    code = start.json()["code"]
    assert len(code) == 6 and code.isdigit()

    consume = await client.post(
        "/api/v1/account-linking/consume",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "channel_type": "line",
            "channel_user_id": "line-user-1",
            "submitted_code": code,
        },
    )
    assert consume.status_code == 201, consume.text
    assert consume.json()["status"] == "active"

    # Confirm it's really in Postgres, not just in the HTTP response — a fresh repository bound
    # to the same session, querying by the real persisted mechanism, not trusting the response.
    identities = PostgresLinkedIdentityRepository(db_session)
    persisted = await identities.get_active_by_channel_user(UUID(installation_id), "line-user-1")
    assert persisted is not None
    assert persisted.prom_user_id == "prom-user-1"


@pytest.mark.asyncio
async def test_full_work_item_journey_with_idempotent_create(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    fake_prom = _seeded_fake_prom_api_client()
    client = _client(db_session, monkeypatch, redis_client, prom_api_client=fake_prom)
    customer_id, instance_id, _workspace_id = await _seed_customer_instance_workspace(client)

    installation = await client.post(
        "/api/v1/installations",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": customer_id,
            "channel_type": "line",
            "display_name": "Acme LINE Bot",
            "credentials": {"channel_access_token": "token-123", "channel_secret": "secret-123"},
        },
    )
    installation_id = installation.json()["id"]

    # A delegated credential is required by the work-item use cases' _resolve() step — seed one
    # directly via the real repository (bound to the same session/transaction), same shortcut
    # tests/application/conftest.py's WorkItemsHarness.link_user takes: this journey is about
    # work-item actions, not re-exercising the OTP flow (that's the linking-journey test's job).
    await _seed_delegated_credential(db_session, customer_id, instance_id, "prom-user-1")

    body = {
        "channel_installation_id": installation_id,
        "prom_instance_id": instance_id,
        "prom_user_id": "prom-user-1",
        "workspace_slug": "acme",
        "project_id": "p1",
        "title": "Ship the E2E tests",
        "state_id": "s1",
        "assignee_ids": ["prom-user-1"],
        "due_date": date(2026, 6, 1).isoformat(),
        "idempotency_key": "e2e-create-1",
    }
    idem_headers = {**_SERVICE_TOKEN_HEADERS, "Idempotency-Key": "hub-idem-key-1"}

    create_1 = await client.post("/api/v1/work-items", headers=idem_headers, json=body)
    assert create_1.status_code == 201, create_1.text
    work_item_id = create_1.json()["work_item"]["id"]
    assert create_1.json()["was_replay"] is False
    calls_after_first = len(fake_prom.calls) + len(fake_prom.list_calls)

    # Same Idempotency-Key + same body -> cached response, no second call to "Prom".
    create_2 = await client.post("/api/v1/work-items", headers=idem_headers, json=body)
    assert create_2.status_code == 201
    assert create_2.json()["work_item"]["id"] == work_item_id
    assert len(fake_prom.calls) + len(fake_prom.list_calls) == calls_after_first

    state_resp = await client.patch(
        f"/api/v1/work-items/{work_item_id}/state",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "prom_instance_id": instance_id,
            "prom_user_id": "prom-user-1",
            "workspace_slug": "acme",
            "project_id": "p1",
            "state_id": "s2",
        },
    )
    assert state_resp.status_code == 200, state_resp.text

    comment_resp = await client.post(
        f"/api/v1/work-items/{work_item_id}/comments",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "prom_instance_id": instance_id,
            "prom_user_id": "prom-user-1",
            "workspace_slug": "acme",
            "project_id": "p1",
            "comment_html": "<p>looks good</p>",
        },
    )
    assert comment_resp.status_code == 201, comment_resp.text

    assignee_resp = await client.patch(
        f"/api/v1/work-items/{work_item_id}/assignees",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "prom_instance_id": instance_id,
            "prom_user_id": "prom-user-1",
            "workspace_slug": "acme",
            "project_id": "p1",
            "assignee_ids": ["prom-user-1"],
        },
    )
    assert assignee_resp.status_code == 200, assignee_resp.text

    complete_resp = await client.post(
        f"/api/v1/work-items/{work_item_id}/complete",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "prom_instance_id": instance_id,
            "prom_user_id": "prom-user-1",
            "workspace_slug": "acme",
            "project_id": "p1",
        },
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert complete_resp.json()["completed_at"] is not None


@pytest.mark.asyncio
async def test_full_notification_journey_with_duplicate_webhook_dedup(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    client = _client(db_session, monkeypatch, redis_client)
    customer_id, instance_id, workspace_id = await _seed_customer_instance_workspace(client)

    installation = await client.post(
        "/api/v1/installations",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": customer_id,
            "channel_type": "line",
            "display_name": "Acme LINE Bot",
            "credentials": {"channel_access_token": "token-123", "channel_secret": "secret-123"},
        },
    )
    installation_id = installation.json()["id"]
    await client.post(
        f"/api/v1/installations/{installation_id}/workspaces",
        headers=_SERVICE_TOKEN_HEADERS,
        json={"workspace_connection_id": workspace_id, "is_default": True},
    )
    start = await client.post(
        "/api/v1/account-linking/start",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "prom_instance_id": instance_id,
            "prom_user_id": "prom-user-1",
            "channel_installation_id": installation_id,
            "delegated_token": "delegated-token",
            "default_workspace_connection_id": workspace_id,
        },
    )
    code = start.json()["code"]
    await client.post(
        "/api/v1/account-linking/consume",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "channel_installation_id": installation_id,
            "channel_type": "line",
            "channel_user_id": "line-user-1",
            "submitted_code": code,
        },
    )

    secret_provider = FernetSecretProvider(get_settings().secret_encryption_key)
    webhook_secret = "plane-webhook-secret"
    patch_resp = await client.patch(
        f"/api/v1/workspace-connections/{workspace_id}/webhook-secret",
        headers=_SERVICE_TOKEN_HEADERS,
        json={"secret": webhook_secret},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    payload = {
        "event": "issue",
        "action": "update",
        "webhook_id": "wh-1",
        "workspace_id": "ws-1",
        "workspace_slug": "acme",
        "data": {"id": "wi-e2e-1", "assignees": [{"id": "prom-user-1"}]},
        "activity": {"field": "assignees", "old_value": [], "new_value": ["prom-user-1"]},
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Plane-Signature": signature,
        "X-Plane-Delivery": "delivery-e2e-1",
        "Content-Type": "application/json",
    }

    sent_tasks: list = []
    monkeypatch.setattr(
        prom_webhook.celery_app, "send_task", lambda name, kwargs: sent_tasks.append((name, kwargs))
    )

    first = await client.post(f"/webhooks/v1/prom/{workspace_id}", content=body, headers=headers)
    assert first.status_code == 200, first.text
    assert len(sent_tasks) == 1
    webhook_event_record_id = sent_tasks[0][1]["webhook_event_record_id"]

    # Directly run what the (unrun, since there's no live Celery worker here) task would do —
    # the real use cases, against the real session, exactly as notification_tasks.py wires them.
    publisher = FakeMessagePublisher()
    fake_clock = FakeClock(datetime.now(UTC))
    schedule_calls: list = []
    process_use_case = ProcessPromWebhookEvent(
        workspace_connections=PostgresWorkspaceConnectionRepository(db_session),
        linked_identities=PostgresLinkedIdentityRepository(db_session),
        notification_preferences=PostgresNotificationPreferenceRepository(db_session),
        outbound_messages=PostgresOutboundMessageRepository(db_session),
        delegated_credentials=PostgresDelegatedCredentialRepository(db_session),
        prom_instances=PostgresPromInstanceRepository(db_session),
        secrets_provider=secret_provider,
        prom_api_client=_seeded_fake_prom_api_client(),
        clock=fake_clock,
        schedule_send=schedule_calls.append,
    )
    await process_use_case.execute(
        workspace_connection_id=UUID(workspace_id),
        payload=payload,
        correlation_id="e2e-test",
    )
    assert len(schedule_calls) == 1

    flush_use_case = FlushOutboundMessage(
        outbound_messages=PostgresOutboundMessageRepository(db_session),
        linked_identities=PostgresLinkedIdentityRepository(db_session),
        installations=PostgresChannelInstallationRepository(db_session),
        secrets_provider=secret_provider,
        publisher=publisher,
        audit_log=PostgresAuditLogRepository(db_session),
        clock=fake_clock,
    )
    await flush_use_case.execute(record_id=schedule_calls[0], correlation_id="e2e-test")
    assert len(publisher.sent) == 1

    # Duplicate delivery: the route must ack 200 but must NOT create a second WebhookEventRecord.
    second = await client.post(f"/webhooks/v1/prom/{workspace_id}", content=body, headers=headers)
    assert second.status_code == 200, second.text
    assert len(sent_tasks) == 1  # still just the one enqueue from the first delivery

    webhook_events = PostgresWebhookEventRepository(db_session)
    record = await webhook_events.get_by_delivery_id("delivery-e2e-1")
    assert record is not None
    assert record.id == UUID(webhook_event_record_id)


@pytest.mark.asyncio
async def test_tenant_isolation_rejects_cross_customer_linking_and_leaks_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    client = _client(db_session, monkeypatch, redis_client)
    customer_a_id, _instance_a_id, workspace_a_id = await _seed_customer_instance_workspace(client)
    _customer_b_id, instance_b_id, _workspace_b_id = await _seed_customer_instance_workspace(client)

    installation_a = await client.post(
        "/api/v1/installations",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "customer_id": customer_a_id,
            "channel_type": "line",
            "display_name": "Acme A LINE Bot",
            "credentials": {"channel_access_token": "token-a", "channel_secret": "secret-a"},
        },
    )
    installation_a_id = installation_a.json()["id"]
    await client.post(
        f"/api/v1/installations/{installation_a_id}/workspaces",
        headers=_SERVICE_TOKEN_HEADERS,
        json={"workspace_connection_id": workspace_a_id, "is_default": True},
    )

    # Customer A's installation, but Customer B's prom_instance -- must be rejected.
    response = await client.post(
        "/api/v1/account-linking/start",
        headers=_SERVICE_TOKEN_HEADERS,
        json={
            "prom_instance_id": instance_b_id,
            "prom_user_id": "prom-user-x",
            "channel_installation_id": installation_a_id,
            "delegated_token": "delegated-token",
            "default_workspace_connection_id": None,
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "TENANT_ISOLATION_VIOLATION"

    # Confirm zero leakage: no LinkingCode, no DelegatedCredential for the attempted user.
    from sqlalchemy import select

    from src.adapters.persistence.models import DelegatedCredentialModel, LinkingCodeModel

    linking_codes = (
        (
            await db_session.execute(
                select(LinkingCodeModel).where(LinkingCodeModel.prom_user_id == "prom-user-x")
            )
        )
        .scalars()
        .all()
    )
    assert linking_codes == []
    credentials = (
        (
            await db_session.execute(
                select(DelegatedCredentialModel).where(
                    DelegatedCredentialModel.prom_user_id == "prom-user-x"
                )
            )
        )
        .scalars()
        .all()
    )
    assert credentials == []


def _seeded_fake_prom_api_client():
    from tests.fakes.support import FakePromApiClient

    client = FakePromApiClient(profile_id_override="prom-user-1")
    client.projects = [ProjectRef(id="p1", name="Engineering", identifier="ENG")]
    client.states_by_project["p1"] = [
        WorkItemState(id="s1", name="Todo", group="unstarted"),
        WorkItemState(id="s2", name="Done", group="completed"),
    ]
    client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="prom-user-1", display_name="Alice")
    ]
    return client


async def _seed_delegated_credential(
    session: AsyncSession, customer_id: str, instance_id: str, prom_user_id: str
) -> None:
    from uuid import UUID as _UUID

    from src.domain.identities.entities import DelegatedCredential

    secret_provider = FernetSecretProvider(get_settings().secret_encryption_key)
    now = datetime.now(UTC)
    repo = PostgresDelegatedCredentialRepository(session)
    await repo.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=_UUID(customer_id),  # real FK: must be a persisted customers row
            prom_instance_id=_UUID(instance_id),
            prom_user_id=prom_user_id,
            encrypted_token=secret_provider.encrypt(b"delegated-token"),
            created_at=now,
            updated_at=now,
            rotated_at=None,
        )
    )
