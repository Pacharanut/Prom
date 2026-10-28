"""AI settings use cases — precedence between saved settings and env defaults, API-key
handling (stored encrypted, never returned), and the settings page's connection test."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from src.shared.errors import AiCredentialInvalidError, AiSettingsInvalidError, NotFoundError
from tests.ai.conftest import AiHarness


async def _save(h: AiHarness, **overrides: object) -> None:
    args: dict[str, object] = {
        "customer_id": h.customer_id,
        "is_enabled": True,
        "base_url": "https://aihub.example/core-api/v1/",
        "model": "gpt-oss-120b",
        "timeout_seconds": 15,
        "api_key": "custom-key",
        "clear_api_key": False,
        "correlation_id": "c",
    }
    args.update(overrides)
    await h.update_settings.execute(**args)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_view_without_saved_settings_shows_env_defaults(ai_harness: AiHarness) -> None:
    view = await ai_harness.get_settings.execute(customer_id=ai_harness.customer_id)
    assert view.is_customized is False
    assert view.is_enabled is False
    assert view.model == "gpt-oss-120b"
    assert view.has_api_key is True and view.api_key_source == "environment"


@pytest.mark.asyncio
async def test_save_encrypts_key_and_never_returns_it(ai_harness: AiHarness) -> None:
    await _save(ai_harness)
    row = await ai_harness.ai_settings.get_by_customer(ai_harness.customer_id)
    assert row is not None
    assert row.base_url == "https://aihub.example/core-api/v1"  # trailing slash trimmed
    assert row.encrypted_api_key is not None and b"custom-key" not in row.encrypted_api_key
    view = await ai_harness.get_settings.execute(customer_id=ai_harness.customer_id)
    assert view.api_key_source == "custom"
    assert "custom-key" not in repr(view)
    assert ai_harness.audit_log.entries[-1].action == "ai_settings.updated"


@pytest.mark.asyncio
async def test_saving_without_key_keeps_the_stored_one(ai_harness: AiHarness) -> None:
    await _save(ai_harness, api_key="first-key")
    await _save(ai_harness, api_key=None, model="Qwen3.8-27B")
    connection = await ai_harness.resolve_connection.execute(ai_harness.customer_id)
    assert connection is not None
    assert connection.api_key == "first-key" and connection.model == "Qwen3.8-27B"


@pytest.mark.asyncio
async def test_clearing_key_falls_back_to_env_key(ai_harness: AiHarness) -> None:
    await _save(ai_harness)
    await _save(ai_harness, api_key=None, clear_api_key=True)
    connection = await ai_harness.resolve_connection.execute(ai_harness.customer_id)
    assert connection is not None and connection.api_key == "env-key"


@pytest.mark.asyncio
async def test_disabled_settings_resolve_to_none(ai_harness: AiHarness) -> None:
    await _save(ai_harness, is_enabled=False)
    assert await ai_harness.resolve_connection.execute(ai_harness.customer_id) is None


@pytest.mark.asyncio
async def test_cannot_enable_without_any_key(ai_harness: AiHarness) -> None:
    ai_harness.defaults = replace(ai_harness.defaults, api_key=None)
    with pytest.raises(AiSettingsInvalidError):
        await _save(ai_harness, api_key=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [{"base_url": "aihub.example"}, {"model": "  "}, {"timeout_seconds": 0}],
)
async def test_invalid_values_are_rejected(ai_harness: AiHarness, overrides: dict) -> None:
    with pytest.raises(AiSettingsInvalidError):
        await _save(ai_harness, **overrides)


@pytest.mark.asyncio
async def test_unknown_customer(ai_harness: AiHarness) -> None:
    with pytest.raises(NotFoundError):
        await ai_harness.get_settings.execute(customer_id=uuid4())


@pytest.mark.asyncio
async def test_connection_test_success_lists_chat_models_only(ai_harness: AiHarness) -> None:
    ai_harness.says("งานของฉันวันนี้", intent="list_my_tasks", filter="today")
    result = await ai_harness.test_connection.execute(customer_id=ai_harness.customer_id)
    assert result.is_ok is True
    assert result.models == ["gpt-oss-120b"]
    assert result.model_found is True
    assert result.sample_intent == "list_my_tasks"
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_connection_test_uses_unsaved_values(ai_harness: AiHarness) -> None:
    await ai_harness.test_connection.execute(
        customer_id=ai_harness.customer_id,
        base_url="https://other.example/v1",
        api_key="draft-key",
        model="Qwen3.8-27B",
    )
    connection = ai_harness.llm_client.calls[0][0]
    assert connection.base_url == "https://other.example/v1"
    assert connection.api_key == "draft-key"
    assert connection.model == "Qwen3.8-27B"


@pytest.mark.asyncio
async def test_connection_test_reports_errors_instead_of_raising(ai_harness: AiHarness) -> None:
    ai_harness.llm_client.models_error = AiCredentialInvalidError("bad key")
    result = await ai_harness.test_connection.execute(customer_id=ai_harness.customer_id)
    assert result.is_ok is False
    assert result.error_code == "AI_CREDENTIAL_INVALID"


@pytest.mark.asyncio
async def test_connection_test_flags_unknown_model(ai_harness: AiHarness) -> None:
    result = await ai_harness.test_connection.execute(
        customer_id=ai_harness.customer_id, model="no-such-model"
    )
    assert result.model_found is False
