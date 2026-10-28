"""Phase 2B: BuildDailySummary, the summary path of CheckDueDateNotifications, notification
preference use cases, and NotificationsPanel. Clock is 2026-01-01 00:00 UTC (07:00 Bangkok)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

from src.application.notifications.panel import NotificationsPanel
from src.application.notifications.preferences import (
    CONFIGURABLE_EVENT_TYPES,
    GetNotificationPreferences,
    UpdateNotificationPreferences,
)
from src.application.notifications.summary import BuildDailySummary, DailySummary
from src.application.work_items.use_cases import FindBlockedWorkItems
from src.channels.notification_copy import compose_daily_summary
from src.domain.identities.entities import DelegatedCredential, IdentityStatus
from src.domain.integrations.entities import ChannelType, InstallationWorkspace
from src.domain.notifications.entities import (
    NotificationEventType,
    OutboundMessageRecord,
    OutboundMessageStatus,
)
from src.domain.work_items.entities import ProjectRef, WorkItem
from src.shared.errors import NotFoundError
from tests.application.conftest import NotificationsHarness

PROM_USER = "prom-user-a"
TODAY = date(2026, 1, 1)


async def _setup(h: NotificationsHarness, *, link_workspace_via: str = "identity"):  # type: ignore[no-untyped-def]
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    workspace = await h.seed_workspace_connection(customer.id, instance.id)
    installation = await h.seed_installation(customer.id, ChannelType.LINE)
    identity = await h.link_user(
        customer_id=customer.id,
        channel_installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER,
        channel_user_id="line-user-a",
    )
    if link_workspace_via == "identity":
        identity.default_workspace_connection_id = workspace.id
        await h.linked_identities.save(identity)
    elif link_workspace_via == "installation_link":
        await h.installations.add_workspace_link(
            InstallationWorkspace(
                id=uuid4(),
                channel_installation_id=installation.id,
                workspace_connection_id=workspace.id,
                created_at=h.clock.now(),
            )
        )
    now = h.clock.now()
    await h.delegated_credentials.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER,
            encrypted_token=h.secrets_provider.encrypt(b"delegated-token"),
            created_at=now,
            updated_at=now,
            rotated_at=None,
        )
    )
    h.prom_api_client.projects = [ProjectRef(id="p1", name="Engineering", identifier="ENG")]
    return identity, workspace


def _item(
    h: NotificationsHarness,
    item_id: str,
    seq: int,
    *,
    due: date | None,
    done: bool = False,
    assignees: list[str] | None = None,
) -> WorkItem:
    item = WorkItem(
        id=item_id,
        sequence_id=seq,
        project_id="p1",
        name=f"Task {seq}",
        state_id="s1",
        priority=None,
        assignee_ids=assignees if assignees is not None else [PROM_USER],
        target_date=due,
        completed_at=datetime(2025, 12, 31) if done else None,
    )
    h.prom_api_client.work_items[item_id] = item
    return item


def _uc(h: NotificationsHarness, cls):  # type: ignore[no-untyped-def]
    return cls(
        prom_instances=h.prom_instances,
        delegated_credentials=h.delegated_credentials,
        secrets_provider=h.secrets_provider,
        prom_api_client=h.prom_api_client,
        audit_log=h.audit_log,
        clock=h.clock,
    )


def _builder(h: NotificationsHarness) -> BuildDailySummary:
    from src.application.work_items.use_cases import ListMyWorkItems, ListProjects

    return BuildDailySummary(
        list_projects=_uc(h, ListProjects),
        list_my_work_items=_uc(h, ListMyWorkItems),
        find_blocked_work_items=_uc(h, FindBlockedWorkItems),
        outbound_messages=h.outbound_messages,
        notification_preferences=h.notification_preferences,
        clock=h.clock,
    )


async def _assigned_record(h: NotificationsHarness, identity_id, work_item_id: str, hours_ago: int):  # type: ignore[no-untyped-def]
    await h.outbound_messages.add(
        OutboundMessageRecord(
            id=uuid4(),
            linked_identity_id=identity_id,
            work_item_id=work_item_id,
            event_types_batched=[NotificationEventType.ASSIGNED],
            status=OutboundMessageStatus.SENT,
            attempt_count=1,
            scheduled_for=h.clock.now(),
            sent_at=h.clock.now(),
            created_at=h.clock.now() - timedelta(hours=hours_ago),
        )
    )


# --- BuildDailySummary -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_sections(notifications_harness: NotificationsHarness) -> None:
    h = notifications_harness
    identity, workspace = await _setup(h)
    _item(h, "late", 1, due=TODAY - timedelta(days=2))
    _item(h, "today", 2, due=TODAY)
    _item(h, "tomorrow", 3, due=TODAY + timedelta(days=1))
    _item(h, "later", 4, due=TODAY + timedelta(days=9))
    _item(h, "done-late", 5, due=TODAY - timedelta(days=1), done=True)
    _item(h, "not-mine", 6, due=TODAY, assignees=["someone-else"])
    _item(h, "new", 7, due=None)
    _item(h, "blocker-open", 8, due=None, assignees=["someone-else"])
    await _assigned_record(h, identity.id, "new", hours_ago=3)
    await _assigned_record(h, identity.id, "later", hours_ago=30)  # outside the 24h window
    h.prom_api_client.blocked_by = {"later": [("p1", "blocker-open")]}

    summary = await _builder(h).execute(
        identity=identity, workspace_slug=workspace.prom_workspace_slug, correlation_id="c"
    )

    assert summary.day == TODAY
    assert [i.work_item_id for i in summary.overdue] == ["late"]
    assert [i.work_item_id for i in summary.due_today] == ["today"]
    assert [i.work_item_id for i in summary.due_soon] == ["tomorrow"]
    assert [i.work_item_id for i in summary.recently_assigned] == ["new"]
    assert [i.work_item_id for i in summary.blocked] == ["later"]
    assert summary.open_count == 5
    assert summary.overdue[0].label == "ENG-1"


@pytest.mark.asyncio
async def test_finished_blocker_does_not_count(notifications_harness: NotificationsHarness) -> None:
    h = notifications_harness
    identity, workspace = await _setup(h)
    _item(h, "a", 1, due=None)
    _item(h, "blocker-done", 2, due=None, done=True, assignees=["x"])
    h.prom_api_client.blocked_by = {"a": [("p1", "blocker-done")]}
    summary = await _builder(h).execute(
        identity=identity, workspace_slug=workspace.prom_workspace_slug, correlation_id="c"
    )
    assert summary.blocked == []


@pytest.mark.asyncio
async def test_muted_types_drop_their_sections(notifications_harness: NotificationsHarness) -> None:
    h = notifications_harness
    identity, workspace = await _setup(h)
    _item(h, "late", 1, due=TODAY - timedelta(days=1))
    _item(h, "today", 2, due=TODAY)
    await h.notification_preferences.set_enabled(
        identity.customer_id, identity.id, NotificationEventType.OVERDUE, False
    )
    summary = await _builder(h).execute(
        identity=identity, workspace_slug=workspace.prom_workspace_slug, correlation_id="c"
    )
    assert summary.overdue == [] and [i.work_item_id for i in summary.due_today] == ["today"]


def test_compose_text_lists_sections_and_empty_message() -> None:
    from src.application.notifications.summary import SummaryItem

    item = SummaryItem(project_id="p", work_item_id="w", label="ENG-1", name="Fix", due_date=TODAY)
    text = compose_daily_summary(DailySummary(day=TODAY, overdue=[item], open_count=3))
    assert "เลยกำหนด (1)" in text and "ENG-1: Fix" in text and "ครบกำหนดวันนี้" not in text
    empty = compose_daily_summary(DailySummary(day=TODAY, open_count=2))
    assert "ไม่มีงานค้าง" in empty


# --- CheckDueDateNotifications with summaries --------------------------------------------------


def _check(h: NotificationsHarness, *, with_summary: bool = True):  # type: ignore[no-untyped-def]
    use_case = h.check_due_dates
    if with_summary:
        use_case.build_daily_summary = _builder(h)
        use_case.compose_summary = compose_daily_summary
    return use_case


@pytest.mark.asyncio
async def test_morning_run_sends_one_summary_instead_of_per_item(
    notifications_harness: NotificationsHarness,
) -> None:
    h = notifications_harness
    await _setup(h)
    _item(h, "late", 1, due=TODAY - timedelta(days=1))
    _item(h, "today", 2, due=TODAY)
    _item(h, "tomorrow", 3, due=TODAY + timedelta(days=1))

    await _check(h).execute(correlation_id="s1")
    assert len(h.publisher.sent) == 1
    assert "สรุปงานของคุณ" in h.publisher.sent[0][2]

    await _check(h).execute(correlation_id="s2")  # hourly beat again, same day
    assert len(h.publisher.sent) == 1


@pytest.mark.asyncio
async def test_summary_switched_off_falls_back_to_per_item(
    notifications_harness: NotificationsHarness,
) -> None:
    h = notifications_harness
    identity, _ = await _setup(h)
    _item(h, "late", 1, due=TODAY - timedelta(days=1))
    _item(h, "today", 2, due=TODAY)
    await h.notification_preferences.set_enabled(
        identity.customer_id, identity.id, NotificationEventType.DAILY_SUMMARY, False
    )
    await _check(h).execute(correlation_id="s1")
    assert len(h.publisher.sent) == 2
    assert all("สรุปงานของคุณ" not in text for _c, _u, text in h.publisher.sent)


@pytest.mark.asyncio
async def test_nothing_to_report_sends_nothing(notifications_harness: NotificationsHarness) -> None:
    h = notifications_harness
    await _setup(h)
    _item(h, "later", 1, due=TODAY + timedelta(days=10))
    await _check(h).execute(correlation_id="s1")
    assert h.publisher.sent == []


@pytest.mark.asyncio
async def test_identity_without_own_default_uses_installation_workspace(
    notifications_harness: NotificationsHarness,
) -> None:
    """The bug fixed in 2B: identities linked via Connected Accounts have no identity-level
    default workspace and used to be skipped entirely by the morning run."""
    h = notifications_harness
    identity, _ = await _setup(h, link_workspace_via="installation_link")
    assert identity.default_workspace_connection_id is None
    _item(h, "today", 1, due=TODAY)
    await _check(h).execute(correlation_id="s1")
    assert len(h.publisher.sent) == 1


# --- Preferences and panel ---------------------------------------------------------------------


def _prefs(
    h: NotificationsHarness,
) -> tuple[GetNotificationPreferences, UpdateNotificationPreferences]:
    return (
        GetNotificationPreferences(
            linked_identities=h.linked_identities,
            notification_preferences=h.notification_preferences,
        ),
        UpdateNotificationPreferences(
            linked_identities=h.linked_identities,
            notification_preferences=h.notification_preferences,
            audit_log=h.audit_log,
            clock=h.clock,
        ),
    )


@pytest.mark.asyncio
async def test_preferences_default_on_and_update(
    notifications_harness: NotificationsHarness,
) -> None:
    h = notifications_harness
    identity, _ = await _setup(h)
    get, update = _prefs(h)

    views = await get.execute(linked_identity_id=identity.id)
    assert [v.event_type for v in views] == list(CONFIGURABLE_EVENT_TYPES)
    assert all(v.enabled for v in views)
    assert set(CONFIGURABLE_EVENT_TYPES) == set(NotificationEventType)

    views = await update.execute(
        linked_identity_id=identity.id,
        changes={NotificationEventType.COMMENT: False},
        correlation_id="c",
    )
    assert {v.event_type for v in views if not v.enabled} == {NotificationEventType.COMMENT}
    assert h.audit_log.entries[-1].action == "notification_preferences.updated"


@pytest.mark.asyncio
async def test_preferences_for_unlinked_identity_are_not_found(
    notifications_harness: NotificationsHarness,
) -> None:
    h = notifications_harness
    identity, _ = await _setup(h)
    identity.status = IdentityStatus.UNLINKED
    await h.linked_identities.save(identity)
    get, _ = _prefs(h)
    with pytest.raises(NotFoundError):
        await get.execute(linked_identity_id=identity.id)


@pytest.mark.asyncio
async def test_panel_toggle_flips_and_ignores_forged_values(
    notifications_harness: NotificationsHarness,
) -> None:
    h = notifications_harness
    identity, _ = await _setup(h)
    get, update = _prefs(h)
    panel = NotificationsPanel(
        get_preferences=get, update_preferences=update, build_daily_summary=_builder(h)
    )
    views = await panel.toggle(identity.id, "overdue", "c")
    assert next(v for v in views if v.event_type == NotificationEventType.OVERDUE).enabled is False
    views = await panel.toggle(identity.id, "overdue", "c")
    assert all(v.enabled for v in views)
    views = await panel.toggle(identity.id, "drop_tables", "c")
    assert all(v.enabled for v in views)
