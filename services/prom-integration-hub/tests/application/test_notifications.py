"""Unit tests for Gate 7's notification pipeline, using in-memory fakes only (no infra).

Covers: `determine_notifications` (pure payload -> pending-notifications mapping),
`ProcessPromWebhookEvent` (who to notify + debounce buffering), `FlushOutboundMessage` (the
actual send + retry bookkeeping), `SendPrivateNotification` (the Gate 5 primitive both of the
above sit on), and `CheckDueDateNotifications` (the scheduled due/overdue sweep).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.application.notifications.use_cases import determine_notifications
from src.domain.identities.entities import DelegatedCredential, IdentityStatus, LinkedIdentity
from src.domain.integrations.entities import ChannelType, InstallationStatus
from src.domain.notifications.entities import NotificationEventType, OutboundMessageStatus
from src.domain.work_items.entities import ProjectRef, WorkItem
from src.shared.errors import ChannelDeliveryFailedError, IntegrationInactiveError, NotFoundError
from tests.application.conftest import NotificationsHarness

PROM_USER_A = "prom-user-a"
PROM_USER_B = "prom-user-b"


# --- determine_notifications (pure function) -----------------------------------------------


def test_issue_created_notifies_assignees() -> None:
    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}, {"id": PROM_USER_B}]},
    }
    work_item_id, notes = determine_notifications(payload)
    assert work_item_id == "wi-1"
    assert {(n.prom_user_id, n.event_type) for n in notes} == {
        (PROM_USER_A, NotificationEventType.ASSIGNED),
        (PROM_USER_B, NotificationEventType.ASSIGNED),
    }


def test_assignee_diff_produces_assigned_and_unassigned() -> None:
    payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_B}]},
        "activity": {
            "field": "assignee_ids",
            "old_value": [PROM_USER_A],
            "new_value": [PROM_USER_B],
        },
    }
    work_item_id, notes = determine_notifications(payload)
    assert work_item_id == "wi-1"
    assert {(n.prom_user_id, n.event_type) for n in notes} == {
        (PROM_USER_B, NotificationEventType.ASSIGNED),
        (PROM_USER_A, NotificationEventType.UNASSIGNED),
    }


def test_state_change_to_non_completed_group_notifies_state_changed() -> None:
    payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}], "state": {"group": "started"}},
        "activity": {"field": "state_id", "old_value": "s0", "new_value": "s1"},
    }
    work_item_id, notes = determine_notifications(payload)
    assert work_item_id == "wi-1"
    assert len(notes) == 1
    assert notes[0].prom_user_id == PROM_USER_A
    assert notes[0].event_type == NotificationEventType.STATE_CHANGED


def test_state_change_to_completed_group_notifies_completed() -> None:
    payload = {
        "event": "issue",
        "action": "updated",
        "data": {
            "id": "wi-1",
            "assignees": [{"id": PROM_USER_A}],
            "state": {"group": "completed"},
        },
        "activity": {"field": "state_id", "old_value": "s0", "new_value": "s1"},
    }
    _, notes = determine_notifications(payload)
    assert notes[0].event_type == NotificationEventType.COMPLETED


def test_issue_comment_create_returns_work_item_id_but_no_notes_yet() -> None:
    """The caller (ProcessPromWebhookEvent) resolves assignees via a live Prom API lookup — see
    test_comment_notifies_current_assignees below — determine_notifications alone can't know
    them from the comment payload."""
    payload = {"event": "issue_comment", "action": "created", "data": {"id": "c1", "issue": "wi-1"}}
    work_item_id, notes = determine_notifications(payload)
    assert work_item_id == "wi-1"
    assert notes == []


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "project", "action": "updated", "data": {"id": "p1"}},
        {"event": "module", "action": "created", "data": {"id": "m1"}},
        {"event": "cycle", "action": "deleted", "data": {"id": "c1"}},
        {"event": "issue", "action": "deleted", "data": {"id": "wi-1"}},
        {
            "event": "issue",
            "action": "updated",
            "data": {"id": "wi-1"},
            "activity": {"field": "priority", "old_value": "low", "new_value": "high"},
        },
    ],
)
def test_out_of_scope_events_produce_no_notifications(payload: dict) -> None:
    """§8.6's trigger list is entirely work-item-centric — project/module/cycle events and
    non-notification-relevant issue field changes (e.g. priority) are scope discipline, not a
    missed case."""
    _work_item_id, notes = determine_notifications(payload)
    assert notes == []


# --- ProcessPromWebhookEvent (fakes) -------------------------------------------------------


async def _seed_linked_setup(harness: NotificationsHarness):
    customer = await harness.seed_customer()
    instance = await harness.seed_prom_instance(customer.id)
    workspace = await harness.seed_workspace_connection(customer.id, instance.id)
    installation = await harness.seed_installation(customer.id, ChannelType.LINE)
    identity = await harness.link_user(
        customer_id=customer.id,
        channel_installation_id=installation.id,
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_A,
        channel_user_id="line-user-a",
    )
    # A real LinkedIdentity always has a DelegatedCredential (Gate 3's StartAccountLinking/
    # ConsumeLinkingCode always create one together) — needed here because
    # CheckDueDateNotifications's list_my_work_items call resolves one via
    # WorkItemsHarness-equivalent _resolve(); without it every call fails with
    # IdentityNotLinkedError, silently swallowed by CheckDueDateNotifications's
    # `except DomainError: continue`, producing zero matches with no visible error.
    now = harness.clock.now()
    await harness.delegated_credentials.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER_A,
            encrypted_token=harness.secrets_provider.encrypt(b"delegated-token"),
            created_at=now,
            updated_at=now,
            rotated_at=None,
        )
    )
    return customer, instance, workspace, installation, identity


@pytest.mark.asyncio
async def test_assignee_added_buffers_notification_for_new_assignee_only(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, instance, workspace, _installation, identity = await _seed_linked_setup(harness)

    payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
        "activity": {"field": "assignee_ids", "old_value": [], "new_value": [PROM_USER_A]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="corr-1"
    )

    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None
    assert record.event_types_batched == [NotificationEventType.ASSIGNED]
    assert harness.scheduled_sends == [record.id]
    assert instance.id  # sanity: instance actually seeded/used


@pytest.mark.asyncio
async def test_assignee_removed_buffers_unassigned(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)

    payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": []},
        "activity": {"field": "assignee_ids", "old_value": [PROM_USER_A], "new_value": []},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="corr-1"
    )

    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None
    assert record.event_types_batched == [NotificationEventType.UNASSIGNED]


@pytest.mark.asyncio
async def test_state_change_notifies_current_assignees(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)

    payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}], "state": {"group": "started"}},
        "activity": {"field": "state_id", "old_value": "s0", "new_value": "s1"},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="corr-1"
    )

    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None
    assert record.event_types_batched == [NotificationEventType.STATE_CHANGED]


@pytest.mark.asyncio
async def test_comment_notifies_current_assignees_via_live_lookup(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    customer, instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    await harness.link_user(
        customer_id=customer.id,
        channel_installation_id=(await harness.seed_installation(customer.id)).id,
        prom_instance_id=instance.id,
        prom_user_id="unrelated-user",
        channel_user_id="line-user-unrelated",
    )
    # DelegatedCredentialRepository.get_any_for_instance's docstring explains why "any" is
    # correct here (we don't yet know who to notify, that's what this lookup determines) —
    # _seed_linked_setup already seeded one for PROM_USER_A, reused as that "any" credential.
    harness.prom_api_client.work_items["wi-1"] = WorkItem(
        id="wi-1",
        sequence_id=1,
        project_id="p1",
        name="Fix bug",
        state_id="s1",
        priority=None,
        assignee_ids=[PROM_USER_A],
        target_date=None,
        completed_at=None,
    )

    payload = {
        "event": "issue_comment",
        "action": "created",
        "data": {"id": "c1", "issue": "wi-1", "project": "p1"},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="corr-1"
    )

    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None
    assert record.event_types_batched == [NotificationEventType.COMMENT]


@pytest.mark.asyncio
async def test_disabled_preference_suppresses_notification(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    await harness.notification_preferences.set_enabled(
        customer.id, identity.id, NotificationEventType.ASSIGNED, enabled=False
    )

    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="corr-1"
    )

    assert await harness.outbound_messages.get_pending(identity.id, "wi-1") is None
    assert harness.scheduled_sends == []


@pytest.mark.asyncio
async def test_user_linked_on_two_channels_both_get_buffered(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    customer, instance, workspace, _line_installation, line_identity = await _seed_linked_setup(
        harness
    )
    discord_installation = await harness.seed_installation(customer.id, ChannelType.DISCORD)
    discord_identity = await harness.link_user(
        customer_id=customer.id,
        channel_installation_id=discord_installation.id,
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_A,  # same Prom user, different channel
        channel_type=ChannelType.DISCORD,
        channel_user_id="discord-user-a",
    )

    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="corr-1"
    )

    assert await harness.outbound_messages.get_pending(line_identity.id, "wi-1") is not None
    assert await harness.outbound_messages.get_pending(discord_identity.id, "wi-1") is not None
    assert len(harness.scheduled_sends) == 2


@pytest.mark.asyncio
async def test_unknown_workspace_connection_is_a_no_op(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=uuid4(), payload=payload, correlation_id="corr-1"
    )
    assert harness.scheduled_sends == []


# --- Debounce ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_events_within_window_collapse_into_one_batched_send(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)

    state_change_payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}], "state": {"group": "started"}},
        "activity": {"field": "state_id", "old_value": "s0", "new_value": "s1"},
    }
    assignee_payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
        "activity": {"field": "assignee_ids", "old_value": [], "new_value": [PROM_USER_A]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=state_change_payload, correlation_id="c1"
    )
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=assignee_payload, correlation_id="c2"
    )

    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None
    assert set(record.event_types_batched) == {
        NotificationEventType.STATE_CHANGED,
        NotificationEventType.ASSIGNED,
    }
    # Only ONE record was ever scheduled — the second event found the first still pending and
    # appended to it rather than creating (and scheduling) a second one.
    assert harness.scheduled_sends == [record.id]


@pytest.mark.asyncio
async def test_event_after_a_completed_send_gets_a_fresh_record(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)

    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="c1"
    )
    first_record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert first_record is not None
    await harness.flush_outbound_message.execute(record_id=first_record.id, correlation_id="c1")
    assert (await harness.outbound_messages.get(first_record.id)).status == (
        OutboundMessageStatus.SENT
    )

    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="c2"
    )
    second_record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert second_record is not None
    assert second_record.id != first_record.id
    assert harness.scheduled_sends == [first_record.id, second_record.id]


# --- FlushOutboundMessage ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_sends_composed_message_and_marks_sent(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="c1"
    )
    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None

    await harness.flush_outbound_message.execute(record_id=record.id, correlation_id="c1")

    assert len(harness.publisher.sent) == 1
    channel_type, channel_user_id, text = harness.publisher.sent[0]
    assert channel_type is ChannelType.LINE
    assert channel_user_id == "line-user-a"
    assert "wi-1" in text
    saved = await harness.outbound_messages.get(record.id)
    assert saved.status is OutboundMessageStatus.SENT
    assert saved.sent_at is not None


@pytest.mark.asyncio
async def test_flush_of_already_sent_record_is_a_no_op(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="c1"
    )
    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    await harness.flush_outbound_message.execute(record_id=record.id, correlation_id="c1")

    # A stray/duplicate Celery delivery of the same countdown task.
    await harness.flush_outbound_message.execute(record_id=record.id, correlation_id="c1-retry")

    assert len(harness.publisher.sent) == 1  # not sent twice


# --- Retry / dead-letter (application-layer logic only — Celery's own retry/backoff scheduling
# is infrastructure glue exercised in src/infrastructure/messaging/notification_tasks.py, not
# testable here without a broker; see that module and README "Known limitations") ---------------


@pytest.mark.asyncio
async def test_publisher_failure_increments_attempt_count_and_reraises(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "wi-1", "assignees": [{"id": PROM_USER_A}]},
    }
    await harness.process_webhook_event.execute(
        workspace_connection_id=workspace.id, payload=payload, correlation_id="c1"
    )
    record = await harness.outbound_messages.get_pending(identity.id, "wi-1")
    assert record is not None

    harness.publisher.fail_next = 1
    with pytest.raises(ChannelDeliveryFailedError):
        await harness.flush_outbound_message.execute(record_id=record.id, correlation_id="c1")

    saved = await harness.outbound_messages.get(record.id)
    assert saved.attempt_count == 1
    assert saved.status is OutboundMessageStatus.PENDING  # not dead-lettered here — that's the
    # Celery task's job once max_retries is exhausted (see notification_tasks.py)

    # A subsequent retry (Celery re-invoking the same task) succeeds once the publisher recovers.
    await harness.flush_outbound_message.execute(record_id=record.id, correlation_id="c1-retry")
    saved_again = await harness.outbound_messages.get(record.id)
    assert saved_again.status is OutboundMessageStatus.SENT
    assert len(harness.publisher.sent) == 1


# --- SendPrivateNotification (the Gate 5 primitive) -----------------------------------------


@pytest.mark.asyncio
async def test_send_private_notification_happy_path(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, _instance, _workspace, _installation, identity = await _seed_linked_setup(harness)

    await harness.send_private_notification.execute(
        linked_identity=identity, text="hello", correlation_id="c1"
    )

    assert harness.publisher.sent == [(ChannelType.LINE, "line-user-a", "hello")]
    success = harness.audit_log.entries_for_action("notification.delivered")
    assert len(success) == 1


@pytest.mark.asyncio
async def test_send_private_notification_installation_not_found(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    _customer, instance, _workspace, _installation, _identity = await _seed_linked_setup(harness)
    ghost_identity = LinkedIdentity(
        id=uuid4(),
        customer_id=uuid4(),
        channel_installation_id=uuid4(),  # no such installation
        channel_type=ChannelType.LINE,
        channel_user_id="line-user-ghost",
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_A,
        default_workspace_connection_id=None,
        status=IdentityStatus.ACTIVE,
        linked_at=harness.clock.now(),
        unlinked_at=None,
    )

    with pytest.raises(NotFoundError):
        await harness.send_private_notification.execute(
            linked_identity=ghost_identity, text="hello", correlation_id="c1"
        )
    assert harness.audit_log.entries_for_action("notification.failed")[-1].error_code == (
        NotFoundError.error_code
    )


@pytest.mark.asyncio
async def test_send_private_notification_inactive_installation(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    customer, instance, _workspace, installation, identity = await _seed_linked_setup(harness)
    installation.status = InstallationStatus.REVOKED
    await harness.installations.save(installation)

    with pytest.raises(IntegrationInactiveError):
        await harness.send_private_notification.execute(
            linked_identity=identity, text="hello", correlation_id="c1"
        )


# --- CheckDueDateNotifications (scheduler body) ----------------------------------------------


@pytest.mark.asyncio
async def test_due_today_item_notified_once(notifications_harness: NotificationsHarness) -> None:
    harness = notifications_harness
    customer, instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    identity.default_workspace_connection_id = workspace.id
    await harness.linked_identities.save(identity)

    harness.prom_api_client.projects = [ProjectRef(id="p1", name="Engineering", identifier="ENG")]
    today = harness.clock.now().date()
    harness.prom_api_client.work_items["wi-1"] = WorkItem(
        id="wi-1",
        sequence_id=1,
        project_id="p1",
        name="Ship it",
        state_id="s1",
        priority=None,
        assignee_ids=[PROM_USER_A],
        target_date=today,
        completed_at=None,
    )

    await harness.check_due_dates.execute(correlation_id="sched-1")

    assert len(harness.publisher.sent) >= 1
    assert any("Ship it" in text for _c, _u, text in harness.publisher.sent)


@pytest.mark.asyncio
async def test_due_date_dedup_prevents_second_send_same_day(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    customer, instance, workspace, _installation, identity = await _seed_linked_setup(harness)
    identity.default_workspace_connection_id = workspace.id
    await harness.linked_identities.save(identity)

    harness.prom_api_client.projects = [ProjectRef(id="p1", name="Engineering", identifier="ENG")]
    today = harness.clock.now().date()
    harness.prom_api_client.work_items["wi-1"] = WorkItem(
        id="wi-1",
        sequence_id=1,
        project_id="p1",
        name="Ship it",
        state_id="s1",
        priority=None,
        assignee_ids=[PROM_USER_A],
        target_date=today,
        completed_at=None,
    )

    await harness.check_due_dates.execute(correlation_id="sched-1")
    first_count = len(harness.publisher.sent)
    assert first_count > 0

    # Simulate the beat task firing again later the same day.
    await harness.check_due_dates.execute(correlation_id="sched-2")

    assert len(harness.publisher.sent) == first_count  # no additional sends


@pytest.mark.asyncio
async def test_identity_without_default_workspace_is_skipped(
    notifications_harness: NotificationsHarness,
) -> None:
    harness = notifications_harness
    await _seed_linked_setup(harness)  # default_workspace_connection_id left None

    # Must not raise even though no workspace is resolvable for this identity.
    await harness.check_due_dates.execute(correlation_id="sched-1")
    assert harness.publisher.sent == []
