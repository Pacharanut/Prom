"""Phase 2E: attributing Plane history to LINE / Discord / Hub / Plane, and the timeline use
case with its filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.application.work_items.timeline import GetWorkItemTimeline, attribute
from src.channels.timeline_copy import entry_line, timeline_text
from src.domain.audit.entities import AuditLogEntry, ResultStatus
from src.domain.integrations.entities import ChannelType
from src.domain.work_items.entities import ProjectMemberRef, WorkItemActivity
from tests.application.conftest import WorkItemsHarness

T0 = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def _activity(
    field: str | None,
    *,
    actor: str = "u-praew",
    at: datetime = T0,
    verb: str = "updated",
    old: str | None = None,
    new: str | None = None,
) -> WorkItemActivity:
    return WorkItemActivity(
        id=str(uuid4()),
        verb=verb,
        field=field,
        old_value=old,
        new_value=new,
        actor_id=actor,
        created_at=at,
    )


def _hub(
    action: str,
    *,
    channel: ChannelType | None = ChannelType.LINE,
    actor: str = "u-praew",
    at: datetime = T0,
    ok: bool = True,
    target: str = "wi-1",
) -> AuditLogEntry:
    return AuditLogEntry(
        id=uuid4(),
        customer_id=uuid4(),
        prom_instance_id=uuid4(),
        installation_id=uuid4() if channel else None,
        channel_type=channel,
        channel_user_id="U1" if channel else None,
        prom_user_id=actor,
        action=action,
        target_type="work_item",
        target_id=target,
        correlation_id="c",
        result_status=ResultStatus.SUCCESS if ok else ResultStatus.FAILURE,
        error_code=None,
        occurred_at=at,
    )


def test_attribution_rules() -> None:
    line_state = _hub("work_item.completed", at=T0 + timedelta(seconds=3))
    assert attribute(_activity("state"), [line_state]) == "line"
    discord = _hub("work_item.assigned", channel=ChannelType.DISCORD)
    assert attribute(_activity("assignees"), [discord]) == "discord"
    api = _hub("work_item.rescheduled", channel=None)
    assert attribute(_activity("target_date"), [api]) == "hub"
    # Same user, same field, but long after the Hub write → done in Plane's web app.
    assert attribute(_activity("state", at=T0 + timedelta(minutes=10)), [line_state]) == "plane"
    # Different person changed it at the same moment → still Plane.
    assert attribute(_activity("state", actor="u-nok"), [line_state]) == "plane"
    # A Hub write of a different kind doesn't claim this activity.
    assert attribute(_activity("priority"), [line_state]) == "plane"
    # A create also produces the initial state/assignee/label activities.
    created = _hub("work_item.created")
    assert attribute(_activity(None, verb="created"), [created]) == "line"
    assert attribute(_activity("labels"), [created]) == "line"
    # The Hub never renames — a rename right after a Hub create happened elsewhere.
    assert attribute(_activity("name"), [created]) == "plane"
    assert attribute(_activity("comment", verb="created"), [_hub("comment.created")]) == "line"


@pytest.mark.asyncio
async def test_timeline_use_case_merges_names_sources_and_filters(
    work_items_harness: WorkItemsHarness,
) -> None:
    h = work_items_harness
    customer_id = uuid4()
    instance = await h.seed_prom_instance(customer_id)
    await h.link_user(customer_id=customer_id, prom_instance_id=instance.id, prom_user_id="u-praew")
    h.prom_api_client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="u-praew", display_name="Praew")
    ]
    h.prom_api_client.activities["wi-1"] = [
        _activity(None, verb="created", at=T0),
        _activity("state", at=T0 + timedelta(hours=1), old="Todo", new="Done"),
        _activity("priority", at=T0 + timedelta(hours=2), old="none", new="high"),
    ]
    await h.audit_log.add(_hub("work_item.created", at=T0 + timedelta(seconds=1)))
    await h.audit_log.add(_hub("work_item.completed", at=T0 + timedelta(hours=1, seconds=2)))
    await h.audit_log.add(
        _hub("work_item.updated", at=T0 + timedelta(hours=2), ok=False)
    )  # failed write
    use_case = GetWorkItemTimeline(
        prom_instances=h.prom_instances,
        delegated_credentials=h.delegated_credentials,
        secrets_provider=h.secrets_provider,
        prom_api_client=h.prom_api_client,
        audit_log=h.audit_log,
        clock=h.clock,
    )

    entries = await use_case.execute(
        prom_instance_id=instance.id,
        prom_user_id="u-praew",
        workspace_slug="acme",
        project_id="p1",
        work_item_id="wi-1",
        correlation_id="c",
    )
    assert [(e.kind, e.source, e.actor_name) for e in entries] == [
        ("priority", "plane", "Praew"),
        ("state", "line", "Praew"),
        ("created", "line", "Praew"),
    ]

    only_line = await use_case.execute(
        prom_instance_id=instance.id,
        prom_user_id="u-praew",
        workspace_slug="acme",
        project_id="p1",
        work_item_id="wi-1",
        source="line",
        kind="state",
        correlation_id="c",
    )
    assert [e.kind for e in only_line] == ["state"]


def test_timeline_copy() -> None:
    from src.application.work_items.timeline import TimelineEntry

    entry = TimelineEntry(
        at=T0,
        source="line",
        kind="state",
        actor_id="u",
        actor_name="Praew",
        old_value="Todo",
        new_value="Done",
    )
    assert entry_line(entry) == "26 ต.ค. 14:00 · 💬 LINE · Praew — สถานะ: Todo → Done"
    assert "ยังไม่มีประวัติ" in timeline_text("AI-1 Fix", [])
    priority = TimelineEntry(
        at=T0,
        source="plane",
        kind="priority",
        actor_id=None,
        actor_name=None,
        old_value=None,
        new_value="urgent",
    )
    assert entry_line(priority).endswith("ไม่ทราบชื่อ — Priority: + ด่วนมาก")
