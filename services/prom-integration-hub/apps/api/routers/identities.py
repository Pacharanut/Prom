"""Unlink a LinkedIdentity."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from apps.api.dependencies import (
    get_get_notification_preferences,
    get_unlink_identity,
    get_update_notification_preferences,
    require_service_token,
)
from apps.api.schemas import (
    LinkedIdentityResponse,
    NotificationPreferenceItem,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdateRequest,
)
from src.application.account_linking.use_cases import UnlinkIdentity
from src.application.notifications.preferences import (
    GetNotificationPreferences,
    NotificationPreferenceView,
    UpdateNotificationPreferences,
)
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


def _preferences_response(
    identity_id: UUID, views: list[NotificationPreferenceView]
) -> NotificationPreferencesResponse:
    return NotificationPreferencesResponse(
        linked_identity_id=identity_id,
        preferences=[
            NotificationPreferenceItem(event_type=v.event_type, enabled=v.enabled) for v in views
        ],
    )


@router.get(
    "/{identity_id}/notification-preferences", response_model=NotificationPreferencesResponse
)
async def get_notification_preferences(
    identity_id: UUID,
    use_case: Annotated[GetNotificationPreferences, Depends(get_get_notification_preferences)],
) -> NotificationPreferencesResponse:
    """Phase 2B. Like DELETE above, Hub trusts its caller here — prom_bridge checks the identity
    belongs to the signed-in Plane user before relaying."""
    views = await use_case.execute(linked_identity_id=identity_id)
    return _preferences_response(identity_id, views)


@router.put(
    "/{identity_id}/notification-preferences", response_model=NotificationPreferencesResponse
)
async def update_notification_preferences(
    identity_id: UUID,
    body: NotificationPreferencesUpdateRequest,
    use_case: Annotated[
        UpdateNotificationPreferences, Depends(get_update_notification_preferences)
    ],
) -> NotificationPreferencesResponse:
    views = await use_case.execute(
        linked_identity_id=identity_id,
        changes=dict(body.preferences),
        correlation_id=get_correlation_id() or "unknown",
    )
    return _preferences_response(identity_id, views)
