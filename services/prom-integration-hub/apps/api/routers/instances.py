"""PromInstance creation (minimal — see routers/customers.py for the same rationale) plus
(Gate 8) listing a Prom user's linked identities, for the "Connected Accounts" admin/profile
view."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends

from apps.api.dependencies import (
    get_linked_identity_repository,
    get_prom_instance_repository,
    require_service_token,
)
from apps.api.schemas import LinkedIdentityResponse, PromInstanceCreateRequest, PromInstanceResponse
from src.adapters.clock import SystemClock
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.domain.instances.entities import InstanceStatus, PromInstance

router = APIRouter(
    prefix="/api/v1/prom-instances",
    tags=["prom-instances"],
    dependencies=[Depends(require_service_token)],
)


@router.post("", response_model=PromInstanceResponse, status_code=201)
async def create_prom_instance(
    body: PromInstanceCreateRequest,
    repository: Annotated[PostgresPromInstanceRepository, Depends(get_prom_instance_repository)],
) -> PromInstanceResponse:
    now = SystemClock().now()
    instance = PromInstance(
        id=uuid4(),
        customer_id=body.customer_id,
        name=body.name,
        base_url=body.base_url,
        status=InstanceStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    await repository.add(instance)
    return PromInstanceResponse(
        id=instance.id,
        customer_id=instance.customer_id,
        name=instance.name,
        base_url=instance.base_url,
        status=instance.status,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
    )


@router.get(
    "/{prom_instance_id}/prom-users/{prom_user_id}/linked-identities",
    response_model=list[LinkedIdentityResponse],
)
async def list_linked_identities_for_prom_user(
    prom_instance_id: UUID,
    prom_user_id: str,
    repository: Annotated[
        PostgresLinkedIdentityRepository, Depends(get_linked_identity_repository)
    ],
) -> list[LinkedIdentityResponse]:
    """Gate 8: "Connected Accounts" — every active channel (LINE, Discord, ...) this Prom user
    has linked, across all installations on this instance. Reuses
    `list_active_by_prom_user` (built in Gate 7 for notification fan-out)."""
    identities = await repository.list_active_by_prom_user(prom_instance_id, prom_user_id)
    return [
        LinkedIdentityResponse(
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
        for identity in identities
    ]
