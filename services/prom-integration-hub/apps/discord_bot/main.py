"""Discord bot process entrypoint — one long-running asyncio process, entirely separate from
`apps/api` (FastAPI/HTTP) and `apps/worker`/`apps/scheduler` (Celery). Discord's plain-DM-message
requirement (master prompt §7's "ส่ง Code ผ่าน private chat/DM") can only be satisfied over
Discord's Gateway (persistent WebSocket) — the HTTP Interactions Endpoint model only ever delivers
slash-command/component interactions, never freeform DM text. See
src/channels/discord/gateway_client.py's module docstring for the per-installation Client wiring.

Run with: python -m apps.discord_bot.main

Starts one discord.py Client per ACTIVE Discord ChannelInstallation found at startup, all running
concurrently in this one process (`asyncio.gather`). Does **not** pick up installations created,
activated, or revoked after startup — restart the process to pick up changes. This is a deliberate
Phase 1 scope cut, not an oversight (see README "Discord bot setup").

No DB session is held open for the process's lifetime: `_make_handler_factory` opens a fresh,
short-lived session (via `get_session_factory()`) for *every single inbound Discord event* — same
session-per-operation discipline as `apps/api`'s FastAPI dependency giving each HTTP request its
own session. A connection that can live for hours/days holding one shared session the whole time
would be a correctness and connection-pool-exhaustion risk.

`_make_handler_factory`'s factory is an async context manager (commit on clean exit, rollback +
re-raise on exception) — the exact same shape as `src/infrastructure/database/session.py`'s
`get_session()`, and for the same reason: a session used as a bare `async with` block only
*closes* on exit, it does not commit, so without this every real write (account linking, work-item
creation) made through the Discord bot would be silently discarded, and worse, left the session
"idle in transaction" indefinitely — found via live testing 2026-08-24: three real linking-code
submissions each left an uncommitted `INSERT INTO linked_identities` open, and every subsequent
attempt hung forever waiting on the resulting lock (one for 29+ minutes) until the whole account-
linking path was wedged. An earlier version of this file left the session on the handler's own
lifetime and reasoned this was "acceptable... not treated as a bug" — that reasoning is what
caused this; not corrected until this real failure surfaced it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.ai.openai_compatible import OpenAICompatibleLlmClient
from src.adapters.clock import SystemClock
from src.adapters.persistence.ai_settings_repository import PostgresAiSettingsRepository
from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.classification_repository import PostgresClassificationRepository
from src.adapters.persistence.delegated_credential_repository import (
    PostgresDelegatedCredentialRepository,
)
from src.adapters.persistence.group_binding_repository import PostgresGroupBindingRepository
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.linking_code_repository import PostgresLinkingCodeRepository
from src.adapters.persistence.notification_preference_repository import (
    PostgresNotificationPreferenceRepository,
)
from src.adapters.persistence.outbound_message_repository import PostgresOutboundMessageRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
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
from src.application.account_linking.use_cases import ConsumeLinkingCode
from src.application.ai.assistant import AiAssistant
from src.application.ai.intent_parser import IntentParser
from src.application.ai.ports import AiDefaults
from src.application.ai.settings import ResolveAiConnection
from src.application.classification.classifier import AutoClassifier, SuggestClassification
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
from src.channels.discord.gateway_client import HandlerFactory, build_client
from src.channels.discord.handler import DiscordWebhookHandler
from src.domain.integrations.entities import ChannelInstallation, ChannelType
from src.infrastructure.configuration.settings import get_settings
from src.infrastructure.database.session import get_session_factory
from src.infrastructure.observability.logging import configure_logging

logger = logging.getLogger(__name__)


def _get_secret_provider() -> FernetSecretProvider:
    return FernetSecretProvider(get_settings().secret_encryption_key)


def _get_prom_api_client() -> HttpPromApiClient:
    return HttpPromApiClient()


def _make_handler_factory() -> HandlerFactory:
    """Returns a factory whose call is an async context manager — entering it opens one fresh
    AsyncSession and wires every port a DiscordWebhookHandler needs against it (mirrors
    apps/api/dependencies.py's get_line_webhook_handler almost exactly), yielding the handler for
    the caller (gateway_client.py) to drive one event's worth of work; exiting commits (or rolls
    back, on exception) and closes. See this module's docstring for why the commit matters."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[DiscordWebhookHandler]:
        session_factory = get_session_factory()
        async with session_factory() as session:
            handler = _build_handler(session)
            try:
                yield handler
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return factory


def _build_handler(session: AsyncSession) -> DiscordWebhookHandler:
    secrets_provider = _get_secret_provider()
    prom_api_client = _get_prom_api_client()
    clock = SystemClock()
    audit_log = PostgresAuditLogRepository(session)
    prom_instances = PostgresPromInstanceRepository(session)
    delegated_credentials = PostgresDelegatedCredentialRepository(session)
    channel_installations = PostgresChannelInstallationRepository(session)
    workspace_connections = PostgresWorkspaceConnectionRepository(session)
    linked_identities = PostgresLinkedIdentityRepository(session)
    linking_codes = PostgresLinkingCodeRepository(session)

    # Every work-item use case takes the same five ports (see apps/api/dependencies.py's
    # identical comment on get_create_work_item et al.) — spelled out explicitly per use case
    # rather than dict-unpacked, to keep this constructible under mypy strict without a pile
    # of per-call type: ignore comments.
    list_projects = ListProjects(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    list_states = ListStates(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    list_project_members = ListProjectMembers(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    create_work_item = CreateWorkItem(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    list_my_work_items = ListMyWorkItems(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    update_work_item_state = UpdateWorkItemState(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    change_assignee = ChangeAssignee(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    add_comment = AddComment(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    mark_complete = MarkComplete(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )

    consume_linking_code = ConsumeLinkingCode(
        installations=channel_installations,
        linking_codes=linking_codes,
        linked_identities=linked_identities,
        rate_limiter=RedisRateLimiter(get_redis_client()),
        audit_log=audit_log,
        clock=clock,
    )

    list_project_work_items = ListProjectWorkItems(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    search_work_items = SearchWorkItems(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    get_work_item = GetWorkItem(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    reschedule_work_item = RescheduleWorkItem(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )

    settings = get_settings()
    llm_client = OpenAICompatibleLlmClient()
    resolve_connection = ResolveAiConnection(
        ai_settings=PostgresAiSettingsRepository(session),
        secrets_provider=secrets_provider,
        defaults=AiDefaults(
            is_enabled=settings.ai_enabled,
            base_url=settings.nt_ai_hub_base_url,
            api_key=settings.nt_ai_hub_key,
            model=settings.nt_ai_hub_model,
            timeout_seconds=settings.ai_timeout_seconds,
        ),
    )
    auto_classifier = AutoClassifier(
        list_labels=ListLabels(
            prom_instances=prom_instances,
            delegated_credentials=delegated_credentials,
            secrets_provider=secrets_provider,
            prom_api_client=prom_api_client,
            audit_log=audit_log,
            clock=clock,
        ),
        suggest=SuggestClassification(
            classification=PostgresClassificationRepository(session), llm_client=llm_client
        ),
        resolve_connection=resolve_connection,
    )
    timeline = GetWorkItemTimeline(
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        audit_log=audit_log,
        clock=clock,
    )
    assistant = AiAssistant(
        resolve_connection=resolve_connection,
        intent_parser=IntentParser(llm_client=llm_client),
        conversation_states=RedisConversationStateRepository(get_redis_client()),
        focus_store=RedisFocusStore(get_redis_client()),
        rate_limiter=RedisRateLimiter(get_redis_client()),
        prom_instances=prom_instances,
        audit_log=audit_log,
        clock=clock,
        list_projects=list_projects,
        list_states=list_states,
        list_project_members=list_project_members,
        list_my_work_items=list_my_work_items,
        list_project_work_items=list_project_work_items,
        search_work_items=search_work_items,
        get_work_item=get_work_item,
        create_work_item=create_work_item,
        update_work_item_state=update_work_item_state,
        mark_complete=mark_complete,
        change_assignee=change_assignee,
        reschedule_work_item=reschedule_work_item,
        add_comment=add_comment,
        auto_classifier=auto_classifier,
        get_timeline=timeline,
    )

    notification_preferences = PostgresNotificationPreferenceRepository(session)
    notifications = NotificationsPanel(
        get_preferences=GetNotificationPreferences(
            linked_identities=linked_identities,
            notification_preferences=notification_preferences,
        ),
        update_preferences=UpdateNotificationPreferences(
            linked_identities=linked_identities,
            notification_preferences=notification_preferences,
            audit_log=audit_log,
            clock=clock,
        ),
        build_daily_summary=BuildDailySummary(
            list_projects=list_projects,
            list_my_work_items=list_my_work_items,
            find_blocked_work_items=FindBlockedWorkItems(
                prom_instances=prom_instances,
                delegated_credentials=delegated_credentials,
                secrets_provider=secrets_provider,
                prom_api_client=prom_api_client,
                audit_log=audit_log,
                clock=clock,
            ),
            outbound_messages=PostgresOutboundMessageRepository(session),
            notification_preferences=notification_preferences,
            clock=clock,
        ),
    )

    return DiscordWebhookHandler(
        linked_identities=linked_identities,
        channel_installations=channel_installations,
        workspace_connections=workspace_connections,
        delegated_credentials=delegated_credentials,
        prom_instances=prom_instances,
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        consume_linking_code=consume_linking_code,
        dedup_store=RedisWebhookDedupStore(get_redis_client()),
        conversation_states=RedisConversationStateRepository(get_redis_client()),
        create_flow=CreateWorkItemFlow(
            list_projects=list_projects,
            list_states=list_states,
            list_project_members=list_project_members,
            create_work_item=create_work_item,
            auto_classifier=auto_classifier,
        ),
        update_flow=UpdateWorkItemFlow(
            list_states=list_states,
            list_project_members=list_project_members,
            update_work_item_state=update_work_item_state,
            change_assignee=change_assignee,
            add_comment=add_comment,
            mark_complete=mark_complete,
        ),
        list_projects=list_projects,
        list_my_work_items=list_my_work_items,
        assistant=assistant,
        notifications=notifications,
        timeline=timeline,
        groups=GroupBindingService(
            bindings=PostgresGroupBindingRepository(session),
            list_projects=list_projects,
            audit_log=audit_log,
            clock=clock,
        ),
    )


async def _load_active_installations() -> list[ChannelInstallation]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        repo = PostgresChannelInstallationRepository(session)
        return await repo.list_active_by_channel_type(ChannelType.DISCORD)


async def _run() -> None:
    configure_logging(get_settings().log_level)
    installations = await _load_active_installations()
    if not installations:
        logger.warning("no active Discord installations found — nothing to start, exiting")
        return

    secrets_provider = _get_secret_provider()
    clients = []
    tokens = []
    for installation in installations:
        credentials: dict[str, str] = json.loads(
            secrets_provider.decrypt(installation.encrypted_credentials).decode("utf-8")
        )
        bot_token = credentials.get("bot_token")
        if not bot_token:
            logger.error(
                "installation %s has no bot_token in its credentials — skipping", installation.id
            )
            continue
        client = build_client(installation, _make_handler_factory())
        clients.append(client)
        tokens.append(bot_token)
        logger.info("starting Discord client for installation %s", installation.id)

    await asyncio.gather(
        *(client.start(token) for client, token in zip(clients, tokens, strict=True))
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
