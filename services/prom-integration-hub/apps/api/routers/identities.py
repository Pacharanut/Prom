"""Unlink a LinkedIdentity."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from apps.api.dependencies import get_unlink_identity, require_service_token
from apps.api.schemas import LinkedIdentityResponse
from src.application.account_linking.use_cases import UnlinkIdentity
from src.infrastructure.observability.logging import get_correlation_id

router = APIRouter(
    prefix="/api/v1/identities", tags=["identities"], dependencies=[Depends(require_service_token)]
)


@router.delete("/{identity_id}", response_model=LinkedIdentityResponse)
async def unlink_identity(
    identity_id: UUID,
    use_case: Annotated[UnlinkIdentity, Depends(get_unlink_identity)],
) -> LinkedIdentityResponse:
    identity = await use_case.execute(
        linked_identity_id=identity_id, correlation_id=get_correlation_id() or "unknown"
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
