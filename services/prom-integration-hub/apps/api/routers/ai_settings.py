"""AI assistant settings for one customer (Phase 2) — what Plane's Settings → AI Assistant page
reads, saves, and tests through prom_bridge. Service-token protected like every /api/v1 route;
the plaintext API key is accepted on write and never returned."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from apps.api.dependencies import (
    get_get_ai_settings,
    get_test_ai_connection,
    get_update_ai_settings,
    require_service_token,
)
from apps.api.schemas import (
    AiConnectionTestResponse,
    AiSettingsResponse,
    AiSettingsTestRequest,
    AiSettingsUpdateRequest,
)
from src.application.ai.settings import (
    AiSettingsView,
    GetAiSettings,
    TestAiConnection,
    UpdateAiSettings,
)
from src.infrastructure.observability.logging import get_correlation_id

router = APIRouter(
    prefix="/api/v1/customers/{customer_id}/ai-settings",
    tags=["ai-settings"],
    dependencies=[Depends(require_service_token)],
)


def _to_response(view: AiSettingsView) -> AiSettingsResponse:
    return AiSettingsResponse(
        customer_id=view.customer_id,
        is_enabled=view.is_enabled,
        base_url=view.base_url,
        model=view.model,
        timeout_seconds=view.timeout_seconds,
        has_api_key=view.has_api_key,
        api_key_source=view.api_key_source,
        is_customized=view.is_customized,
    )


@router.get("", response_model=AiSettingsResponse)
async def get_ai_settings(
    customer_id: UUID,
    use_case: Annotated[GetAiSettings, Depends(get_get_ai_settings)],
) -> AiSettingsResponse:
    return _to_response(await use_case.execute(customer_id=customer_id))


@router.put("", response_model=AiSettingsResponse)
async def update_ai_settings(
    customer_id: UUID,
    body: AiSettingsUpdateRequest,
    use_case: Annotated[UpdateAiSettings, Depends(get_update_ai_settings)],
) -> AiSettingsResponse:
    view = await use_case.execute(
        customer_id=customer_id,
        is_enabled=body.is_enabled,
        base_url=body.base_url,
        model=body.model,
        timeout_seconds=body.timeout_seconds,
        api_key=body.api_key,
        clear_api_key=body.clear_api_key,
        correlation_id=get_correlation_id() or "unknown",
    )
    return _to_response(view)


@router.post("/test", response_model=AiConnectionTestResponse)
async def test_ai_connection(
    customer_id: UUID,
    body: AiSettingsTestRequest,
    use_case: Annotated[TestAiConnection, Depends(get_test_ai_connection)],
) -> AiConnectionTestResponse:
    result = await use_case.execute(
        customer_id=customer_id,
        base_url=body.base_url,
        model=body.model,
        timeout_seconds=body.timeout_seconds,
        api_key=body.api_key,
    )
    return AiConnectionTestResponse(
        is_ok=result.is_ok,
        models=result.models,
        model_found=result.model_found,
        latency_ms=result.latency_ms,
        sample_intent=result.sample_intent,
        error_code=result.error_code,
        error_message=result.error_message,
    )
