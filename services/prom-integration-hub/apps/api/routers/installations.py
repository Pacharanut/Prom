"""Channel installation lifecycle: create, link to a workspace, revoke, and (Gate 8) detail,
unlink a workspace, test the connection, and reversible activate/deactivate."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from apps.api.dependencies import (
    get_activate_installation,
    get_channel_installation_repository,
    get_create_channel_installation,
    get_deactivate_installation,
    get_delete_channel_installation,
    get_link_installation_to_workspace,
    get_revoke_installation,
    get_test_channel_connection,
    get_unlink_workspace_from_installation,
    require_service_token,
)
from apps.api.schemas import (
    ChannelInstallationCreateRequest,
    ChannelInstallationDetailResponse,
    ChannelInstallationResponse,
    InstallationWorkspaceLinkRequest,
    InstallationWorkspaceLinkResponse,
    TestConnectionResponse,
)
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.application.account_linking.use_cases import (
    ActivateInstallation,
    CreateChannelInstallation,
    DeactivateInstallation,
    DeleteChannelInstallation,
    LinkInstallationToWorkspace,
    RevokeInstallation,
    TestChannelConnection,
    UnlinkWorkspaceFromInstallation,
)
from src.domain.integrations.entities import ChannelInstallation
from src.infrastructure.observability.logging import get_correlation_id
from src.shared.errors import NotFoundError

router = APIRouter(
    prefix="/api/v1/installations",
    tags=["installations"],
    dependencies=[Depends(require_service_token)],
)


def _correlation_id() -> str:
    return get_correlation_id() or "unknown"


def _to_response(installation: ChannelInstallation) -> ChannelInstallationResponse:
    return ChannelInstallationResponse(
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


@router.post("", response_model=ChannelInstallationResponse, status_code=201)
async def create_installation(
    body: ChannelInstallationCreateRequest,
    use_case: Annotated[CreateChannelInstallation, Depends(get_create_channel_installation)],
) -> ChannelInstallationResponse:
    installation = await use_case.execute(
        customer_id=body.customer_id,
        channel_type=body.channel_type,
        display_name=body.display_name,
        credentials=body.credentials,
        correlation_id=_correlation_id(),
    )
    return _to_response(installation)


@router.post(
    "/{installation_id}/workspaces",
    response_model=InstallationWorkspaceLinkResponse,
    status_code=201,
)
async def link_installation_to_workspace(
    installation_id: UUID,
    body: InstallationWorkspaceLinkRequest,
    use_case: Annotated[LinkInstallationToWorkspace, Depends(get_link_installation_to_workspace)],
) -> InstallationWorkspaceLinkResponse:
    link = await use_case.execute(
        channel_installation_id=installation_id,
        workspace_connection_id=body.workspace_connection_id,
        is_default=body.is_default,
        correlation_id=_correlation_id(),
    )
    return InstallationWorkspaceLinkResponse(
        id=link.id,
        channel_installation_id=link.channel_installation_id,
        workspace_connection_id=link.workspace_connection_id,
        created_at=link.created_at,
    )


@router.post("/{installation_id}/revoke", response_model=ChannelInstallationResponse)
async def revoke_installation(
    installation_id: UUID,
    use_case: Annotated[RevokeInstallation, Depends(get_revoke_installation)],
) -> ChannelInstallationResponse:
    installation = await use_case.execute(
        channel_installation_id=installation_id, correlation_id=_correlation_id()
    )
    return _to_response(installation)


@router.delete("/{installation_id}", status_code=204)
async def delete_installation(
    installation_id: UUID,
    use_case: Annotated[DeleteChannelInstallation, Depends(get_delete_channel_installation)],
) -> None:
    await use_case.execute(
        channel_installation_id=installation_id, correlation_id=_correlation_id()
    )


# --- Gate 8 (Prom UI Integration) ---------------------------------------------------------


@router.get("/{installation_id}", response_model=ChannelInstallationDetailResponse)
async def get_installation(
    installation_id: UUID,
    repository: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
) -> ChannelInstallationDetailResponse:
    installation = await repository.get(installation_id)
    if installation is None:
        raise NotFoundError(f"channel_installation {installation_id} not found")
    links = await repository.list_workspaces(installation_id)
    return ChannelInstallationDetailResponse(
        **_to_response(installation).model_dump(),
        workspaces=[
            InstallationWorkspaceLinkResponse(
                id=link.id,
                channel_installation_id=link.channel_installation_id,
                workspace_connection_id=link.workspace_connection_id,
                created_at=link.created_at,
            )
            for link in links
        ],
    )


@router.delete("/{installation_id}/workspaces/{workspace_connection_id}", status_code=204)
async def unlink_workspace_from_installation(
    installation_id: UUID,
    workspace_connection_id: UUID,
    use_case: Annotated[
        UnlinkWorkspaceFromInstallation, Depends(get_unlink_workspace_from_installation)
    ],
) -> None:
    await use_case.execute(
        channel_installation_id=installation_id,
        workspace_connection_id=workspace_connection_id,
        correlation_id=_correlation_id(),
    )


@router.post("/{installation_id}/test-connection", response_model=TestConnectionResponse)
async def test_installation_connection(
    installation_id: UUID,
    use_case: Annotated[TestChannelConnection, Depends(get_test_channel_connection)],
) -> TestConnectionResponse:
    result = await use_case.execute(
        channel_installation_id=installation_id, correlation_id=_correlation_id()
    )
    return TestConnectionResponse(is_valid=result.is_valid, error_message=result.error_message)


@router.post("/{installation_id}/activate", response_model=ChannelInstallationResponse)
async def activate_installation(
    installation_id: UUID,
    use_case: Annotated[ActivateInstallation, Depends(get_activate_installation)],
) -> ChannelInstallationResponse:
    installation = await use_case.execute(
        channel_installation_id=installation_id, correlation_id=_correlation_id()
    )
    return _to_response(installation)


@router.post("/{installation_id}/deactivate", response_model=ChannelInstallationResponse)
async def deactivate_installation(
    installation_id: UUID,
    use_case: Annotated[DeactivateInstallation, Depends(get_deactivate_installation)],
) -> ChannelInstallationResponse:
    installation = await use_case.execute(
        channel_installation_id=installation_id, correlation_id=_correlation_id()
    )
    return _to_response(installation)
