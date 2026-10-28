"""Phase 2E HTTP tests: per-work-item timeline and the searchable audit log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import get_audit_log_repository, get_get_work_item_timeline
from apps.api.main import create_app
from src.application.work_items.timeline import GetWorkItemTimeline
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.integrations.entities import ChannelType
from src.domain.work_items.entities import ProjectMemberRef, WorkItemActivity
from src.infrastructure.configuration.settings import get_settings
from tests.application.conftest import WorkItemsHarness
from tests.fakes.repositories import (
    FakeAuditLogRepository,
    FakeDelegatedCredentialRepository,
    FakePromInstanceRepository,
)
from tests.fakes.support import FakeClock, FakePromApiClient, FakeSecretProvider

_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
T0 = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def _entry(
    customer_id, action: str, channel: ChannelType | None, at: datetime, target: str = "wi-1"
) -> AuditLogEntry:  # type: ignore[no-untyped-def]
    return AuditLogEntry(
        id=uuid4(),
        customer_id=customer_id,
        prom_instance_id=None,
        installation_id=None,
        channel_type=channel,
        channel_user_id=None,
        prom_user_id="u1",
        action=action,
        target_type="work_item",
        target_id=target,
        correlation_id="c",
        result_status=ResultStatus.SUCCESS,
        error_code=None,
        occurred_at=at,
    )


@pytest.mark.asyncio
async def test_audit_log_filters() -> None:
    customer_id = uuid4()
    audit = FakeAuditLogRepository()
    await audit.add(_entry(customer_id, "work_item.completed", ChannelType.LINE, T0))
    await audit.add(
        _entry(customer_id, "work_item.assigned", ChannelType.DISCORD, T0 + timedelta(hours=1))
    )
    await audit.add(
        _entry(
            customer_id, "identity.linked", ChannelType.LINE, T0 + timedelta(hours=2), target="x"
        )
    )
    app = create_app()
    app.dependency_overrides[get_audit_log_repository] = lambda: audit
    client = TestClient(app)
    url = f"/api/v1/customers/{customer_id}/audit-log"

    def actions(**params: str) -> list[str]:
        response = client.get(url, headers=_HEADERS, params=params)
        assert response.status_code == 200
        return [e["action"] for e in response.json()]

    assert actions(channel_type="line") == ["identity.linked", "work_item.completed"]
    assert actions(action="work_item.") == ["work_item.assigned", "work_item.completed"]
    assert actions(target_id="wi-1", channel_type="discord") == ["work_item.assigned"]
    assert actions(
        occurred_after=(T0 + timedelta(minutes=30)).isoformat(), action="work_item."
    ) == ["work_item.assigned"]


@pytest.mark.asyncio
async def test_timeline_route() -> None:
    h = WorkItemsHarness(
        prom_instances=FakePromInstanceRepository(),
        delegated_credentials=FakeDelegatedCredentialRepository(),
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(T0),
        prom_api_client=FakePromApiClient(),
        secrets_provider=FakeSecretProvider(),
    )
    customer_id = uuid4()
    instance = await h.seed_prom_instance(customer_id)
    await h.link_user(customer_id=customer_id, prom_instance_id=instance.id, prom_user_id="u1")
    h.prom_api_client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="u1", display_name="Praew")
    ]
    h.prom_api_client.activities["wi-1"] = [
        WorkItemActivity(
            id="a",
            verb="updated",
            field="state",
            old_value="Todo",
            new_value="Done",
            actor_id="u1",
            created_at=T0,
        )
    ]
    await h.audit_log.add(
        _entry(customer_id, "work_item.completed", ChannelType.LINE, T0 + timedelta(seconds=5))
    )
    app = create_app()
    app.dependency_overrides[get_get_work_item_timeline] = lambda: GetWorkItemTimeline(
        prom_instances=h.prom_instances,
        delegated_credentials=h.delegated_credentials,
        secrets_provider=h.secrets_provider,
        prom_api_client=h.prom_api_client,
        audit_log=h.audit_log,
        clock=h.clock,
    )
    response = TestClient(app).get(
        "/api/v1/work-items/wi-1/timeline",
        headers=_HEADERS,
        params={
            "prom_instance_id": str(instance.id),
            "prom_user_id": "u1",
            "workspace_slug": "acme",
            "project_id": "p1",
        },
    )
    assert response.status_code == 200
    [entry] = response.json()
    assert (
        entry["source"] == "line"
        and entry["actor_name"] == "Praew"
        and entry["new_value"] == "Done"
    )
