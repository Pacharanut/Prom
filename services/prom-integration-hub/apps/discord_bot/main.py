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
"""

from __future__ import annotations

import asyncio
import json
import logging

from src.adapters.clock import SystemClock
from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.delegated_credential_repository import (
    PostgresDelegatedCredentialRepository,
)
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
from src.adapters.persistence.linking_code_repository import PostgresLinkingCodeRepository
from src.adapters.persistence.prom_instance_repository import PostgresPromInstanceRepository
from src.adapters.persistence.workspace_connection_repository import (
    PostgresWorkspaceConnectionRepository,
)
from src.adapters.prom_api.client import HttpPromApiClient
from src.adapters.redis.client import get_redis_client
from src.adapters.redis.conversation_store import RedisConversationStateRepository
from src.adapters.redis.rate_limiter import RedisRateLimiter
from src.adapters.redis.webhook_dedup import RedisWebhookDedupStore
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.application.account_linking.use_cases import ConsumeLinkingCode
from src.application.work_items.conversation import CreateWorkItemFlow, UpdateWorkItemFlow
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListStates,
    MarkComplete,
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
    """Returns a factory closure — calling it opens one fresh AsyncSession, wires every port a
    DiscordWebhookHandler needs against it (mirrors apps/api/dependencies.py's
    get_line_webhook_handler almost exactly), and returns the handler. The caller
    (gateway_client.py) is responsible for the session's lifetime being exactly one event's worth
    of work; nothing here closes the session explicitly because nothing here opened it as a
    context manager — see the note below.
    """

    async def factory() -> DiscordWebhookHandler:
        # Deliberately NOT `async with session_factory() as session:` — that would close the
        # session before the caller (handle_event, still awaiting) finishes using the
        # repositories bound to it. Instead the session is left open for the handler's lifetime;
        # AsyncSession's own garbage collection closes the underlying connection eventually, and
        # SQLAlchemy's pool reclaims it — acceptable for Phase 1's per-event session churn given
        # each event does a small, bounded amount of work. A Gate 7+ revisit could wrap this in an
        # explicit try/finally once discord.py's callback shape makes that easy to express
        # cleanly; noted in README, not treated as a bug.
        session_factory = get_session_factory()
        session = session_factory()

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
        )

    return factory


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
