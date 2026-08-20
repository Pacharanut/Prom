"""Account-linking flow: start (issue a 6-digit code) and consume (create a LinkedIdentity)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from apps.api.dependencies import (
    get_consume_linking_code,
    get_start_account_linking,
    require_service_token,
)
from apps.api.schemas import (
    ConsumeLinkingCodeRequest,
    LinkedIdentityResponse,
    StartAccountLinkingRequest,
    StartAccountLinkingResponse,
)
from src.application.account_linking.use_cases import ConsumeLinkingCode, StartAccountLinking
from src.infrastructure.observability.logging import get_correlation_id

router = APIRouter(
    prefix="/api/v1/account-linking",
    tags=["account-linking"],
    dependencies=[Depends(require_service_token)],
)


def _correlation_id() -> str:
    return get_correlation_id() or "unknown"


@router.post("/start", response_model=StartAccountLinkingResponse, status_code=201)
async def start_account_linking(
    body: StartAccountLinkingRequest,
    use_case: Annotated[StartAccountLinking, Depends(get_start_account_linking)],
) -> StartAccountLinkingResponse:
    linking_code = await use_case.execute(
        prom_instance_id=body.prom_instance_id,
        prom_user_id=body.prom_user_id,
        channel_installation_id=body.channel_installation_id,
        delegated_token=body.delegated_token,
        default_workspace_connection_id=body.default_workspace_connection_id,
        correlation_id=_correlation_id(),
    )
    return StartAccountLinkingResponse(code=linking_code.code, expires_at=linking_code.expires_at)


@router.post("/consume", response_model=LinkedIdentityResponse, status_code=201)
async def consume_linking_code(
    body: ConsumeLinkingCodeRequest,
    use_case: Annotated[ConsumeLinkingCode, Depends(get_consume_linking_code)],
) -> LinkedIdentityResponse:
    identity = await use_case.execute(
        channel_installation_id=body.channel_installation_id,
        channel_type=body.channel_type,
        channel_user_id=body.channel_user_id,
        submitted_code=body.submitted_code,
        correlation_id=_correlation_id(),
    )
    return LinkedIdentityResponse(
        id=identity.id,
        customer_id=identity.customer_id,
        channel_installation_id=identity.channel_installation_id,
        channel_type=identity.channel_type,
        channel_user_id=identity.channel_user_id,
        prom_instance_id=identity.prom_instance_id,
        prom_user_id=identity.prom_user_id,
        default_workspace_connection_id=identity.default_workspace_connection_id,
        status=identity.status,
        linked_at=identity.linked_at,
        unlinked_at=identity.unlinked_at,
    )
