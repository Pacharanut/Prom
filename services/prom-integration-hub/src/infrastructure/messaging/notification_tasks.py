"""Celery task definitions for Gate 7 (inbound Prom webhook processing, debounced notification
sends, and the due-date/overdue scheduled check). This is the one module allowed to import both
Celery and every adapter/use-case needed to build them — the same "composition root" role
apps/api/dependencies.py plays for the FastAPI process, just for the worker/scheduler processes.
Application-layer code (src/application/notifications/use_cases.py) stays framework-free and
never imports this module; instead this module imports *it* and supplies the `schedule_send`
callable ProcessPromWebhookEvent needs.

Each task opens its own DB session (a Celery worker is a separate process/thread pool from
FastAPI's request-scoped session, so apps/api/dependencies.py's SessionDep pattern doesn't apply
here) and runs the underlying async use case via asyncio.run — the standard way to bridge
Celery's synchronous task model into this codebase's async-everywhere application layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.adapters.clock import SystemClock
from src.adapters.persistence.audit_log_repository import PostgresAuditLogRepository
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.adapters.persistence.delegated_credential_repository import (
    PostgresDelegatedCredentialRepository,
)
from src.adapters.persistence.linked_identity_repository import PostgresLinkedIdentityRepository
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
from src.adapters.redis.webhook_dedup import RedisWebhookDedupStore
from src.adapters.secrets.fernet_provider import FernetSecretProvider
from src.application.notifications.use_cases import (
    CheckDueDateNotifications,
    FlushOutboundMessage,
    ProcessPromWebhookEvent,
)
from src.application.work_items.use_cases import ListMyWorkItems, ListProjects
from src.channels.publisher_registry import MessagePublisherRegistry
from src.domain.notifications.entities import OutboundMessageStatus
from src.domain.webhooks.entities import WebhookEventStatus
from src.infrastructure.configuration.settings import get_settings
from src.infrastructure.messaging.celery_app import celery_app
from src.shared.errors import ChannelDeliveryFailedError

_BANGKOK = ZoneInfo("Asia/Bangkok")
"""Gate 7 Phase 1: a single hardcoded default timezone for every customer's due-date checks, per
the master prompt's explicit instruction — not a per-user/per-workspace preference system. A
deliberate scope decision, not an oversight; see README "Known limitations"."""

_DUE_CHECK_LOCAL_HOUR = 9
"""CheckDueDateNotifications beat task runs hourly (see celery_app.conf.beat_schedule in
apps/scheduler/main.py) but only actually does anything when Asia/Bangkok's local hour equals
this — the simplest way to get "once a day, at a sensible morning time" out of a plain hourly
Celery beat interval without a timezone-aware cron library. Trades a little a precision (could be
off by up to ~30min around DST-less Thailand's fixed UTC+7, so effectively exact) for schedule
simplicity."""

_SECRET_PROVIDER = None


def _secret_provider() -> FernetSecretProvider:
    global _SECRET_PROVIDER
    if _SECRET_PROVIDER is None:
        _SECRET_PROVIDER = FernetSecretProvider(get_settings().secret_encryption_key)
    return _SECRET_PROVIDER


@asynccontextmanager
async def _session_scope() -> AsyncIterator[AsyncSession]:
    """A fresh engine (and session) per call, disposed on exit — deliberately NOT
    `apps/api/dependencies.py`'s cached `get_session_factory()`/`get_engine()` singletons.

    Those are `@lru_cache`d for FastAPI's single long-lived event loop; every task in this module
    runs via its own top-level `asyncio.run(...)` call, which spins a brand new event loop each
    time. A cross-invocation cached engine's connection pool would end up bound to a stale loop
    on the second task run — "Future attached to a different loop" — the exact bug already found
    and fixed in `tests/integration/conftest.py` during Gate 3's verification; same root cause,
    same fix (own engine, disposed after use), applied here before it could ever surface.
    """
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def schedule_debounced_send(record_id: UUID) -> None:
    """The `schedule_send` callable ProcessPromWebhookEvent needs — kept as a plain module-level
    function (not a lambda in the composition site) so it's importable/patchable in tests without
    needing to reach into a closure."""
    send_debounced_notification.apply_async(args=[str(record_id)], countdown=45)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="notifications.process_prom_webhook_event",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def process_prom_webhook_event(
    self: Any,
    *,
    workspace_connection_id: str,
    webhook_event_record_id: str,
    payload: dict[str, Any],
) -> None:
    async def _run() -> None:
        async with _session_scope() as session:
            event_repo = PostgresWebhookEventRepository(session)
            record = await event_repo.get(UUID(webhook_event_record_id))
            if record is None:
                return

            use_case = ProcessPromWebhookEvent(
                workspace_connections=PostgresWorkspaceConnectionRepository(session),
                linked_identities=PostgresLinkedIdentityRepository(session),
                notification_preferences=PostgresNotificationPreferenceRepository(session),
                outbound_messages=PostgresOutboundMessageRepository(session),
                delegated_credentials=PostgresDelegatedCredentialRepository(session),
                prom_instances=PostgresPromInstanceRepository(session),
                secrets_provider=_secret_provider(),
                prom_api_client=HttpPromApiClient(),
                clock=SystemClock(),
                schedule_send=schedule_debounced_send,
            )
            try:
                await use_case.execute(
                    workspace_connection_id=UUID(workspace_connection_id),
                    payload=payload,
                    correlation_id=str(self.request.id or "unknown"),
                )
            except Exception:
                record.status = WebhookEventStatus.FAILED
                record.processed_at = datetime.now(UTC)
                await event_repo.save(record)
                await session.commit()
                raise

            record.status = WebhookEventStatus.PROCESSED
            record.processed_at = datetime.now(UTC)
            await event_repo.save(record)
            await session.commit()

    try:
        asyncio.run(_run())
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            # Retries exhausted — dead-letter the record instead of raising again (which would
            # just get silently dropped once Celery gives up); do this in a fresh session since
            # the one inside _run() may be in a bad state after the exception.
            async def _dead_letter() -> None:
                async with _session_scope() as dl_session:
                    dl_repo = PostgresWebhookEventRepository(dl_session)
                    dl_record = await dl_repo.get(UUID(webhook_event_record_id))
                    if dl_record is not None:
                        dl_record.status = WebhookEventStatus.DEAD_LETTER
                        dl_record.processed_at = datetime.now(UTC)
                        await dl_repo.save(dl_record)
                        await dl_session.commit()

            asyncio.run(_dead_letter())
            return
        raise exc


@celery_app.task(  # type: ignore[untyped-decorator]
    name="notifications.send_debounced_notification",
    bind=True,
    autoretry_for=(ChannelDeliveryFailedError,),
    retry_backoff=True,
    max_retries=5,
)
def send_debounced_notification(self: Any, record_id: str) -> None:
    async def _run() -> None:
        async with _session_scope() as session:
            use_case = FlushOutboundMessage(
                outbound_messages=PostgresOutboundMessageRepository(session),
                linked_identities=PostgresLinkedIdentityRepository(session),
                installations=PostgresChannelInstallationRepository(session),
                secrets_provider=_secret_provider(),
                publisher=MessagePublisherRegistry(),
                audit_log=PostgresAuditLogRepository(session),
                clock=SystemClock(),
            )
            try:
                await use_case.execute(
                    record_id=UUID(record_id), correlation_id=str(self.request.id or "unknown")
                )
                await session.commit()
            except ChannelDeliveryFailedError:
                await session.commit()  # persist the attempt_count bump FlushOutboundMessage made
                raise

    try:
        asyncio.run(_run())
    except ChannelDeliveryFailedError as exc:
        if self.request.retries >= self.max_retries:

            async def _dead_letter() -> None:
                async with _session_scope() as dl_session:
                    repo = PostgresOutboundMessageRepository(dl_session)
                    record = await repo.get(UUID(record_id))
                    if record is not None:
                        record.status = OutboundMessageStatus.DEAD_LETTER
                        await repo.save(record)
                        await dl_session.commit()

            asyncio.run(_dead_letter())
            return
        raise exc


@celery_app.task(name="notifications.check_due_dates")  # type: ignore[untyped-decorator]
def check_due_dates() -> None:
    now_bangkok = datetime.now(_BANGKOK)
    if now_bangkok.hour != _DUE_CHECK_LOCAL_HOUR:
        return

    async def _run() -> None:
        async with _session_scope() as session:
            prom_api_client = HttpPromApiClient()
            audit_log = PostgresAuditLogRepository(session)
            clock = SystemClock()
            use_case = CheckDueDateNotifications(
                linked_identities=PostgresLinkedIdentityRepository(session),
                workspace_connections=PostgresWorkspaceConnectionRepository(session),
                list_projects=ListProjects(
                    prom_instances=PostgresPromInstanceRepository(session),
                    delegated_credentials=PostgresDelegatedCredentialRepository(session),
                    secrets_provider=_secret_provider(),
                    prom_api_client=prom_api_client,
                    audit_log=audit_log,
                    clock=clock,
                ),
                list_my_work_items=ListMyWorkItems(
                    prom_instances=PostgresPromInstanceRepository(session),
                    delegated_credentials=PostgresDelegatedCredentialRepository(session),
                    secrets_provider=_secret_provider(),
                    prom_api_client=prom_api_client,
                    audit_log=audit_log,
                    clock=clock,
                ),
                notification_preferences=PostgresNotificationPreferenceRepository(session),
                installations=PostgresChannelInstallationRepository(session),
                secrets_provider=_secret_provider(),
                publisher=MessagePublisherRegistry(),
                audit_log=audit_log,
                clock=clock,
                dedup_store=RedisWebhookDedupStore(get_redis_client()),
            )
            await use_case.execute(correlation_id="scheduler")
            await session.commit()

    asyncio.run(_run())


__all__ = [
    "check_due_dates",
    "process_prom_webhook_event",
    "schedule_debounced_send",
    "send_debounced_notification",
]
