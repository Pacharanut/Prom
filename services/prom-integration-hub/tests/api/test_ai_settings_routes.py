"""HTTP-level tests for /api/v1/customers/{id}/ai-settings — the endpoints Plane's
Settings → AI Assistant page reaches through prom_bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import (
    get_get_ai_settings,
    get_test_ai_connection,
    get_update_ai_settings,
)
from apps.api.main import create_app
from src.application.ai.intent_parser import IntentParser
from src.application.ai.ports import AiDefaults
from src.application.ai.settings import GetAiSettings, TestAiConnection, UpdateAiSettings
from src.domain.customers.entities import Customer, CustomerStatus
from src.infrastructure.configuration.settings import get_settings
from tests.fakes.repositories import (
    FakeAiSettingsRepository,
    FakeAuditLogRepository,
    FakeCustomerRepository,
)
from tests.fakes.support import FakeClock, FakeLlmClient, FakeSecretProvider

_HEADERS = {"X-Hub-Service-Token": get_settings().hub_service_token}
_NOW = datetime(2026, 10, 26, tzinfo=UTC)
_DEFAULTS = AiDefaults(
    is_enabled=False,
    base_url="https://aihub.example/core-api/v1",
    api_key=None,
    model="gpt-oss-120b",
    timeout_seconds=15,
)


async def _client() -> tuple[TestClient, Customer, FakeAiSettingsRepository, FakeLlmClient]:
    customers = FakeCustomerRepository()
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=_NOW, updated_at=_NOW
    )
    await customers.add(customer)
    ai_settings = FakeAiSettingsRepository()
    secrets_provider = FakeSecretProvider()
    llm = FakeLlmClient(default={"intent": "list_my_tasks", "filter": "today"})
    clock = FakeClock(_NOW)

    app = create_app()
    app.dependency_overrides[get_get_ai_settings] = lambda: GetAiSettings(
        customers=customers, ai_settings=ai_settings, defaults=_DEFAULTS
    )
    app.dependency_overrides[get_update_ai_settings] = lambda: UpdateAiSettings(
        customers=customers,
        ai_settings=ai_settings,
        secrets_provider=secrets_provider,
        defaults=_DEFAULTS,
        audit_log=FakeAuditLogRepository(),
        clock=clock,
    )
    app.dependency_overrides[get_test_ai_connection] = lambda: TestAiConnection(
        customers=customers,
        ai_settings=ai_settings,
        secrets_provider=secrets_provider,
        defaults=_DEFAULTS,
        llm_client=llm,
        intent_parser=IntentParser(llm_client=llm),
        clock=clock,
    )
    return TestClient(app), customer, ai_settings, llm


@pytest.mark.asyncio
async def test_requires_service_token() -> None:
    client, customer, _, _ = await _client()
    response = client.get(f"/api/v1/customers/{customer.id}/ai-settings")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_defaults_then_put_then_get() -> None:
    client, customer, _, _ = await _client()
    url = f"/api/v1/customers/{customer.id}/ai-settings"

    first = client.get(url, headers=_HEADERS)
    assert first.status_code == 200
    assert first.json()["is_customized"] is False
    assert first.json()["has_api_key"] is False

    saved = client.put(
        url,
        headers=_HEADERS,
        json={
            "is_enabled": True,
            "base_url": "https://aihub.example/core-api/v1",
            "model": "gpt-oss-120b",
            "timeout_seconds": 20,
            "api_key": "super-secret",
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["is_enabled"] is True and body["api_key_source"] == "custom"
    assert "super-secret" not in saved.text

    again = client.get(url, headers=_HEADERS)
    assert again.json()["timeout_seconds"] == 20
    assert "super-secret" not in again.text


@pytest.mark.asyncio
async def test_enabling_without_any_key_is_422() -> None:
    client, customer, _, _ = await _client()
    response = client.put(
        f"/api/v1/customers/{customer.id}/ai-settings",
        headers=_HEADERS,
        json={"is_enabled": True, "base_url": "https://aihub.example/v1", "model": "m"},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "AI_SETTINGS_INVALID"


@pytest.mark.asyncio
async def test_unknown_customer_is_404() -> None:
    client, _, _, _ = await _client()
    response = client.get(f"/api/v1/customers/{uuid4()}/ai-settings", headers=_HEADERS)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_connection_test_with_draft_values() -> None:
    client, customer, _, llm = await _client()
    response = client.post(
        f"/api/v1/customers/{customer.id}/ai-settings/test",
        headers=_HEADERS,
        json={"api_key": "draft-key", "model": "gpt-oss-120b"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_ok"] is True
    assert body["models"] == ["gpt-oss-120b"]
    assert body["sample_intent"] == "list_my_tasks"
    assert llm.calls[0][0].api_key == "draft-key"
    assert "draft-key" not in response.text
