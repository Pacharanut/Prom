"""Customer creation (minimal — full customer lifecycle management is out of Gate 3 scope, this
exists so instances/workspaces/installations have a customer to reference) plus (Gate 8) two
customer-scoped admin listing endpoints: installations and audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query

from apps.api.dependencies import (
    get_audit_log_repository,
    get_channel_installation_repository,
    get_customer_repository,
    require_service_token,
)
from apps.api.schemas import (
    AuditLogEntryResponse,
    ChannelInstallationResponse,
    CustomerCreateRequest,
    CustomerResponse,
)
from src.adapters.clock import SystemClock
from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.application.account_linking.ports import AuditLogFilters
from src.domain.audit.entities import ResultStatus
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.integrations.entities import ChannelType

router = APIRouter(
    prefix="/api/v1/customers", tags=["customers"], dependencies=[Depends(require_service_token)]
)


@router.post("", response_model=CustomerResponse, status_code=201)
async def create_customer(
    body: CustomerCreateRequest,
    repository: Annotated[PostgresCustomerRepository, Depends(get_customer_repository)],
) -> CustomerResponse:
    now = SystemClock().now()
    customer = Customer(
        id=uuid4(), name=body.name, status=CustomerStatus.ACTIVE, created_at=now, updated_at=now
    )
    await repository.add(customer)
    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        status=customer.status,
        created_at=customer.created_at,
        updated_at=customer.updated_at,
    )


@router.get("/{customer_id}/installations", response_model=list[ChannelInstallationResponse])
async def list_customer_installations(
    customer_id: UUID,
    repository: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
) -> list[ChannelInstallationResponse]:
    installations = await repository.list_by_customer(customer_id)
    return [
        ChannelInstallationResponse(
            id=installation.id,
            customer_id=installation.customer_id,
            channel_type=installation.channel_type,
            display_name=installation.display_name,
            status=installation.status,
            external_account_id=installation.external_account_id,
            default_workspace_connection_id=installation.default_workspace_connection_id,
            created_at=installation.created_at,
            updated_at=installation.updated_at,
        )
        for installation in installations
    ]


@router.get("/{customer_id}/audit-log", response_model=list[AuditLogEntryResponse])
async def list_customer_audit_log(
    customer_id: UUID,
    repository: Annotated[PostgresAuditLogRepository, Depends(get_audit_log_repository)],
    installation_id: UUID | None = None,
    limit: Annotated[int, Query(le=200, gt=0)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    channel_type: ChannelType | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    target_id: Annotated[str | None, Query(max_length=100)] = None,
    result_status: ResultStatus | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> list[AuditLogEntryResponse]:
    """Phase 2E: optional filters make this the searchable history (`action` is a substring
    match, e.g. "work_item." for every work-item change)."""
    entries = await repository.list_for_customer(
        customer_id,
        installation_id=installation_id,
        limit=limit,
        offset=offset,
        filters=AuditLogFilters(
            channel_type=channel_type,
            action_contains=action,
            target_id=target_id,
            result_status=result_status,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
        ),
    )
    return [
        AuditLogEntryResponse(
            id=entry.id,
            customer_id=entry.customer_id,
            prom_instance_id=entry.prom_instance_id,
            installation_id=entry.installation_id,
            channel_type=entry.channel_type,
            channel_user_id=entry.channel_user_id,
            prom_user_id=entry.prom_user_id,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            correlation_id=entry.correlation_id,
            result_status=entry.result_status,
            error_code=entry.error_code,
            occurred_at=entry.occurred_at,
        )
        for entry in entries
    ]
