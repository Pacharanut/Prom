"""Composition root for the FastAPI app: wires adapters into use cases per request. This is the
one layer allowed to import everything (domain, application, adapters, infrastructure) — see
AGENTS.md.
"""

from __future__ import annotations

import secrets as _secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.ai.openai_compatible import OpenAICompatibleLlmClient
from src.adapters.clock import SystemClock
from src.adapters.persistence.ai_settings_repository import PostgresAiSettingsRepository
from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.classification_repository import PostgresClassificationRepository
from src.adapters.persistence.customer_repository import PostgresCustomerRepository
from src.adapters.persistence.delegated_credential_repository import (
    PostgresDelegatedCredentialRepository,
)
from src.adapters.persistence.group_binding_repository import PostgresGroupBindingRepository
from src.adapters.persistence.idempotency_repository import PostgresIdempotencyRepository
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.linking_code_repository import PostgresLinkingCodeRepository
from src.adapters.persistence.notification_preference_repository import (
    PostgresNotificationPreferenceRepository,
)
from src.adapters.persistence.outbound_message_repository import PostgresOutboundMessageRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.webhook_event_repository import PostgresWebhookEventRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.adapters.prom_api.client import HttpPromApiClient
from src.adapters.redis.client import get_redis_client
from src.adapters.redis.conversation_store import RedisConversationStateRepository
from src.adapters.redis.focus_store import RedisFocusStore
from src.adapters.redis.rate_limiter import RedisRateLimiter
from src.adapters.redis.webhook_dedup import RedisWebhookDedupStore
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.application.account_linking.use_cases import (
    ActivateInstallation,
    ConsumeLinkingCode,
    CreateChannelInstallation,
    DeactivateInstallation,
    DeleteChannelInstallation,
    LinkInstallationToWorkspace,
    RegisterWorkspaceConnection,
    RevokeInstallation,
    StartAccountLinking,
    TestChannelConnection,
    UnlinkIdentity,
    UnlinkWorkspaceFromInstallation,
)
from src.application.ai.assistant import AiAssistant
from src.application.ai.intent_parser import IntentParser
from src.application.ai.ports import AiDefaults
from src.application.ai.settings import (
    GetAiSettings,
    ResolveAiConnection,
    TestAiConnection,
    UpdateAiSettings,
)
from src.application.classification.classifier import AutoClassifier, SuggestClassification
from src.application.classification.settings import (
    GetClassificationSettings,
    UpdateClassificationSettings,
)
from src.application.groups.service import GroupBindingService
from src.application.notifications.panel import NotificationsPanel
from src.application.notifications.preferences import (
    GetNotificationPreferences,
    UpdateNotificationPreferences,
)
from src.application.notifications.summary import BuildDailySummary
from src.application.work_items.conversation import CreateWorkItemFlow, UpdateWorkItemFlow
from src.application.work_items.timeline import GetWorkItemTimeline
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    FindBlockedWorkItems,
    GetWorkItem,
    ListLabels,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListProjectWorkItems,
    ListStates,
    MarkComplete,
    RescheduleWorkItem,
    SearchWorkItems,
    UpdateWorkItemState,
)
from src.channels.line.handler import LineWebhookHandler
from src.channels.validator_registry import ChannelValidatorRegistry
from src.infrastructure.configuration.settings import get_settings
from src.infrastructure.database.session import get_session


async def require_service_token(
    x_hub_service_token: Annotated[str | None, Header()] = None,
) -> None:
    """Rejects any request that doesn't present the shared Hub service token. Models the
    service-to-service half of Hybrid Authentication — end-user-facing auth (LINE/Discord
    webhook signatures) is Gate 5/6 scope, not this."""
    settings = get_settings()
    if x_hub_service_token is None or not _secrets.compare_digest(
        x_hub_service_token, settings.hub_service_token
    ):
        raise HTTPException(status_code=401, detail="missing or invalid X-Hub-Service-Token")


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_secret_provider() -> FernetSecretProvider:
    return FernetSecretProvider(get_settings().secret_encryption_key)


def get_credential_validator() -> ChannelValidatorRegistry:
    return ChannelValidatorRegistry()


def get_prom_api_client() -> HttpPromApiClient:
    return HttpPromApiClient()


def get_rate_limiter() -> RedisRateLimiter:
    return RedisRateLimiter(get_redis_client())


def get_create_channel_installation(session: SessionDep) -> CreateChannelInstallation:
    return CreateChannelInstallation(
        installations=PostgresChannelInstallationRepository(session),
        validator=get_credential_validator(),
        secrets_provider=get_secret_provider(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_register_workspace_connection(session: SessionDep) -> RegisterWorkspaceConnection:
    return RegisterWorkspaceConnection(
        workspace_connections=PostgresWorkspaceConnectionRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_link_installation_to_workspace(session: SessionDep) -> LinkInstallationToWorkspace:
    return LinkInstallationToWorkspace(
        installations=PostgresChannelInstallationRepository(session),
        workspace_connections=PostgresWorkspaceConnectionRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_start_account_linking(session: SessionDep) -> StartAccountLinking:
    return StartAccountLinking(
        installations=PostgresChannelInstallationRepository(session),
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        linking_codes=PostgresLinkingCodeRepository(session),
        prom_api_client=get_prom_api_client(),
        secrets_provider=get_secret_provider(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_consume_linking_code(session: SessionDep) -> ConsumeLinkingCode:
    return ConsumeLinkingCode(
        installations=PostgresChannelInstallationRepository(session),
        linking_codes=PostgresLinkingCodeRepository(session),
        linked_identities=PostgresLinkedIdentityRepository(session),
        rate_limiter=get_rate_limiter(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_unlink_identity(session: SessionDep) -> UnlinkIdentity:
    return UnlinkIdentity(
        linked_identities=PostgresLinkedIdentityRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_revoke_installation(session: SessionDep) -> RevokeInstallation:
    return RevokeInstallation(
        installations=PostgresChannelInstallationRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_delete_channel_installation(session: SessionDep) -> DeleteChannelInstallation:
    return DeleteChannelInstallation(
        installations=PostgresChannelInstallationRepository(session),
        linked_identities=PostgresLinkedIdentityRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


# --- Gate 8 (Prom UI Integration) ---------------------------------------------------------


def get_unlink_workspace_from_installation(
    session: SessionDep,
) -> UnlinkWorkspaceFromInstallation:
    return UnlinkWorkspaceFromInstallation(
        installations=PostgresChannelInstallationRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_test_channel_connection(session: SessionDep) -> TestChannelConnection:
    return TestChannelConnection(
        installations=PostgresChannelInstallationRepository(session),
        validator=get_credential_validator(),
        secrets_provider=get_secret_provider(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_activate_installation(session: SessionDep) -> ActivateInstallation:
    return ActivateInstallation(
        installations=PostgresChannelInstallationRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_deactivate_installation(session: SessionDep) -> DeactivateInstallation:
    return DeactivateInstallation(
        installations=PostgresChannelInstallationRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_audit_log_repository(session: SessionDep) -> PostgresAuditLogRepository:
    return PostgresAuditLogRepository(session)


def get_linked_identity_repository(session: SessionDep) -> PostgresLinkedIdentityRepository:
    return PostgresLinkedIdentityRepository(session)


def get_customer_repository(session: SessionDep) -> PostgresCustomerRepository:
    return PostgresCustomerRepository(session)


def get_prom_instance_repository(session: SessionDep) -> PostgresPromInstanceRepository:
    return PostgresPromInstanceRepository(session)


def get_workspace_connection_repository(
    session: SessionDep,
) -> PostgresWorkspaceConnectionRepository:
    return PostgresWorkspaceConnectionRepository(session)


def get_channel_installation_repository(
    session: SessionDep,
) -> PostgresChannelInstallationRepository:
    return PostgresChannelInstallationRepository(session)


def get_idempotency_repository(session: SessionDep) -> PostgresIdempotencyRepository:
    return PostgresIdempotencyRepository(session)


def get_webhook_event_repository(session: SessionDep) -> PostgresWebhookEventRepository:
    return PostgresWebhookEventRepository(session)


# --- Work items (Gate 4) -------------------------------------------------------------------
# Every factory below wires the same five ports (prom_instances, delegated_credentials,
# secrets_provider, prom_api_client, audit_log, clock) into a different use case — the ports
# are identical because every work-item use case starts by resolving the same delegated
# credential (see resolve_delegated_context), only the PromApiClient call differs.


def get_create_work_item(session: SessionDep) -> CreateWorkItem:
    return CreateWorkItem(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_get_work_item(session: SessionDep) -> GetWorkItem:
    return GetWorkItem(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_search_work_items(session: SessionDep) -> SearchWorkItems:
    return SearchWorkItems(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_list_projects(session: SessionDep) -> ListProjects:
    return ListProjects(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_list_states(session: SessionDep) -> ListStates:
    return ListStates(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_list_project_members(session: SessionDep) -> ListProjectMembers:
    return ListProjectMembers(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_list_my_work_items(session: SessionDep) -> ListMyWorkItems:
    return ListMyWorkItems(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_update_work_item_state(session: SessionDep) -> UpdateWorkItemState:
    return UpdateWorkItemState(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_change_assignee(session: SessionDep) -> ChangeAssignee:
    return ChangeAssignee(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_add_comment(session: SessionDep) -> AddComment:
    return AddComment(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_mark_complete(session: SessionDep) -> MarkComplete:
    return MarkComplete(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_reschedule_work_item(session: SessionDep) -> RescheduleWorkItem:
    return RescheduleWorkItem(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_list_project_work_items(session: SessionDep) -> ListProjectWorkItems:
    return ListProjectWorkItems(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


# --- AI assistant (Phase 2) -------------------------------------------------------------------


def get_ai_defaults() -> AiDefaults:
    settings = get_settings()
    return AiDefaults(
        is_enabled=settings.ai_enabled,
        base_url=settings.nt_ai_hub_base_url,
        api_key=settings.nt_ai_hub_key,
        model=settings.nt_ai_hub_model,
        timeout_seconds=settings.ai_timeout_seconds,
    )


def get_llm_client() -> OpenAICompatibleLlmClient:
    return OpenAICompatibleLlmClient()


def get_resolve_ai_connection(session: SessionDep) -> ResolveAiConnection:
    return ResolveAiConnection(
        ai_settings=PostgresAiSettingsRepository(session),
        secrets_provider=get_secret_provider(),
        defaults=get_ai_defaults(),
    )


def get_get_ai_settings(session: SessionDep) -> GetAiSettings:
    return GetAiSettings(
        customers=PostgresCustomerRepository(session),
        ai_settings=PostgresAiSettingsRepository(session),
        defaults=get_ai_defaults(),
    )


def get_update_ai_settings(session: SessionDep) -> UpdateAiSettings:
    return UpdateAiSettings(
        customers=PostgresCustomerRepository(session),
        ai_settings=PostgresAiSettingsRepository(session),
        secrets_provider=get_secret_provider(),
        defaults=get_ai_defaults(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_test_ai_connection(session: SessionDep) -> TestAiConnection:
    llm_client = get_llm_client()
    return TestAiConnection(
        customers=PostgresCustomerRepository(session),
        ai_settings=PostgresAiSettingsRepository(session),
        secrets_provider=get_secret_provider(),
        defaults=get_ai_defaults(),
        llm_client=llm_client,
        intent_parser=IntentParser(llm_client=llm_client),
        clock=SystemClock(),
    )


def get_get_work_item_timeline(session: SessionDep) -> GetWorkItemTimeline:
    return GetWorkItemTimeline(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_auto_classifier(session: SessionDep) -> AutoClassifier:
    return AutoClassifier(
        list_labels=ListLabels(
            prom_instances=PostgresPromInstanceRepository(session),
            delegated_credentials=PostgresDelegatedCredentialRepository(session),
            secrets_provider=get_secret_provider(),
            prom_api_client=get_prom_api_client(),
            audit_log=PostgresAuditLogRepository(session),
            clock=SystemClock(),
        ),
        suggest=SuggestClassification(
            classification=PostgresClassificationRepository(session),
            llm_client=get_llm_client(),
        ),
        resolve_connection=get_resolve_ai_connection(session),
    )


def get_get_classification_settings(session: SessionDep) -> GetClassificationSettings:
    return GetClassificationSettings(
        workspace_connections=PostgresWorkspaceConnectionRepository(session),
        classification=PostgresClassificationRepository(session),
    )


def get_update_classification_settings(session: SessionDep) -> UpdateClassificationSettings:
    return UpdateClassificationSettings(
        workspace_connections=PostgresWorkspaceConnectionRepository(session),
        classification=PostgresClassificationRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_ai_assistant(session: SessionDep) -> AiAssistant:
    redis_client = get_redis_client()
    return AiAssistant(
        resolve_connection=get_resolve_ai_connection(session),
        intent_parser=IntentParser(llm_client=get_llm_client()),
        conversation_states=RedisConversationStateRepository(redis_client),
        focus_store=RedisFocusStore(redis_client),
        rate_limiter=get_rate_limiter(),
        prom_instances=PostgresPromInstanceRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
        list_projects=get_list_projects(session),
        list_states=get_list_states(session),
        list_project_members=get_list_project_members(session),
        list_my_work_items=get_list_my_work_items(session),
        list_project_work_items=get_list_project_work_items(session),
        search_work_items=get_search_work_items(session),
        get_work_item=get_get_work_item(session),
        create_work_item=get_create_work_item(session),
        update_work_item_state=get_update_work_item_state(session),
        mark_complete=get_mark_complete(session),
        change_assignee=get_change_assignee(session),
        reschedule_work_item=get_reschedule_work_item(session),
        add_comment=get_add_comment(session),
        auto_classifier=get_auto_classifier(session),
        get_timeline=get_get_work_item_timeline(session),
    )


# --- Notifications (Phase 2B) -----------------------------------------------------------------


def get_get_notification_preferences(session: SessionDep) -> GetNotificationPreferences:
    return GetNotificationPreferences(
        linked_identities=PostgresLinkedIdentityRepository(session),
        notification_preferences=PostgresNotificationPreferenceRepository(session),
    )


def get_update_notification_preferences(session: SessionDep) -> UpdateNotificationPreferences:
    return UpdateNotificationPreferences(
        linked_identities=PostgresLinkedIdentityRepository(session),
        notification_preferences=PostgresNotificationPreferenceRepository(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_find_blocked_work_items(session: SessionDep) -> FindBlockedWorkItems:
    return FindBlockedWorkItems(
        prom_instances=PostgresPromInstanceRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


def get_notifications_panel(session: SessionDep) -> NotificationsPanel:
    return NotificationsPanel(
        get_preferences=get_get_notification_preferences(session),
        update_preferences=get_update_notification_preferences(session),
        build_daily_summary=BuildDailySummary(
            list_projects=get_list_projects(session),
            list_my_work_items=get_list_my_work_items(session),
            find_blocked_work_items=get_find_blocked_work_items(session),
            outbound_messages=PostgresOutboundMessageRepository(session),
            notification_preferences=PostgresNotificationPreferenceRepository(session),
            clock=SystemClock(),
        ),
    )


def get_group_binding_service(session: SessionDep) -> GroupBindingService:
    return GroupBindingService(
        bindings=PostgresGroupBindingRepository(session),
        list_projects=get_list_projects(session),
        audit_log=PostgresAuditLogRepository(session),
        clock=SystemClock(),
    )


# --- LINE webhook (Gate 5) --------------------------------------------------------------------


def get_line_webhook_handler(session: SessionDep) -> LineWebhookHandler:
    create_flow = CreateWorkItemFlow(
        list_projects=get_list_projects(session),
        list_states=get_list_states(session),
        list_project_members=get_list_project_members(session),
        create_work_item=get_create_work_item(session),
        auto_classifier=get_auto_classifier(session),
    )
    update_flow = UpdateWorkItemFlow(
        list_states=get_list_states(session),
        list_project_members=get_list_project_members(session),
        update_work_item_state=get_update_work_item_state(session),
        change_assignee=get_change_assignee(session),
        add_comment=get_add_comment(session),
        mark_complete=get_mark_complete(session),
    )
    return LineWebhookHandler(
        linked_identities=PostgresLinkedIdentityRepository(session),
        channel_installations=PostgresChannelInstallationRepository(session),
        workspace_connections=PostgresWorkspaceConnectionRepository(session),
        delegated_credentials=PostgresDelegatedCredentialRepository(session),
        prom_instances=PostgresPromInstanceRepository(session),
        secrets_provider=get_secret_provider(),
        prom_api_client=get_prom_api_client(),
        consume_linking_code=get_consume_linking_code(session),
        dedup_store=RedisWebhookDedupStore(get_redis_client()),
        conversation_states=RedisConversationStateRepository(get_redis_client()),
        create_flow=create_flow,
        update_flow=update_flow,
        list_projects=get_list_projects(session),
        list_my_work_items=get_list_my_work_items(session),
        assistant=get_ai_assistant(session),
        notifications=get_notifications_panel(session),
        groups=get_group_binding_service(session),
        timeline=get_get_work_item_timeline(session),
    )
