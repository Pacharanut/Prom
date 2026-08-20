"""Unit tests for the work-item use cases, using in-memory fakes only (no infra)."""

from __future__ import annotations

from datetime import date

import pytest

from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem, WorkItemState
from src.shared.errors import IdentityNotLinkedError, PromPermissionDeniedError
from tests.application.conftest import WorkItemsHarness

PROM_USER_ID = "prom-user-1"


@pytest.mark.asyncio
async def test_create_work_item_happy_path(work_items_harness: WorkItemsHarness) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )

    work_item, was_replay = await work_items_harness.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        title="Fix the bug",
        idempotency_key="idem-1",
        correlation_id="corr-1",
    )

    assert work_item.name == "Fix the bug"
    assert was_replay is False
    success = work_items_harness.audit_log.entries_for_action("work_item.created")[-1]
    assert success.result_status.value == "success"


@pytest.mark.asyncio
async def test_create_work_item_idempotent_replay_via_native_external_id(
    work_items_harness: WorkItemsHarness,
) -> None:
    """Two create calls with the same idempotency_key must hit Prom's own external_id dedup
    (simulated for real in FakePromApiClient, not just stubbed) and return the same work item
    with was_replay=True on the second call."""
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )

    first, first_replay = await work_items_harness.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        title="Fix the bug",
        idempotency_key="idem-shared",
        correlation_id="corr-1",
    )
    second, second_replay = await work_items_harness.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        title="Fix the bug (retried)",
        idempotency_key="idem-shared",
        correlation_id="corr-2",
    )

    assert first_replay is False
    assert second_replay is True
    assert second.id == first.id
    replay_entry = work_items_harness.audit_log.entries_for_action("work_item.create_replayed")[-1]
    assert replay_entry.result_status.value == "success"


@pytest.mark.asyncio
async def test_create_work_item_identity_not_linked(work_items_harness: WorkItemsHarness) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    # deliberately not calling link_user

    with pytest.raises(IdentityNotLinkedError):
        await work_items_harness.create_work_item.execute(
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER_ID,
            workspace_slug="acme",
            project_id="proj-1",
            title="Fix the bug",
            correlation_id="corr-1",
        )

    failure = work_items_harness.audit_log.entries_for_action("work_item.create_failed")[-1]
    assert failure.error_code == IdentityNotLinkedError.error_code


@pytest.mark.asyncio
async def test_create_work_item_permission_denied_is_audited(
    work_items_harness: WorkItemsHarness,
) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )
    work_items_harness.prom_api_client.create_error = PromPermissionDeniedError(
        "not a member of this project"
    )

    with pytest.raises(PromPermissionDeniedError):
        await work_items_harness.create_work_item.execute(
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER_ID,
            workspace_slug="acme",
            project_id="proj-1",
            title="Fix the bug",
            correlation_id="corr-1",
        )

    failure = work_items_harness.audit_log.entries_for_action("work_item.create_failed")[-1]
    assert failure.error_code == PromPermissionDeniedError.error_code
    assert failure.customer_id == customer.id


@pytest.mark.asyncio
async def test_update_state_change_assignee_add_comment_happy_paths(
    work_items_harness: WorkItemsHarness,
) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )
    work_item, _ = await work_items_harness.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        title="Fix the bug",
        correlation_id="corr-1",
    )

    updated = await work_items_harness.update_work_item_state.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        work_item_id=work_item.id,
        state_id="state-started",
        correlation_id="corr-2",
    )
    assert updated.state_id == "state-started"
    assert work_items_harness.audit_log.entries_for_action("work_item.updated")

    reassigned = await work_items_harness.change_assignee.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        work_item_id=work_item.id,
        assignee_ids=["user-2"],
        correlation_id="corr-3",
    )
    assert reassigned.assignee_ids == ["user-2"]
    assert work_items_harness.audit_log.entries_for_action("work_item.assigned")

    comment_id = await work_items_harness.add_comment.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        work_item_id=work_item.id,
        comment_html="<p>looks good</p>",
        correlation_id="corr-4",
    )
    assert comment_id
    assert work_items_harness.audit_log.entries_for_action("comment.created")


@pytest.mark.asyncio
async def test_mark_complete_happy_path(work_items_harness: WorkItemsHarness) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )
    work_items_harness.prom_api_client.states_by_project["proj-1"] = [
        WorkItemState(id="state-backlog", name="Backlog", group="backlog"),
        WorkItemState(id="state-done", name="Done", group="completed"),
    ]
    work_item, _ = await work_items_harness.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        title="Fix the bug",
        correlation_id="corr-1",
    )

    completed = await work_items_harness.mark_complete.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        work_item_id=work_item.id,
        correlation_id="corr-2",
    )

    assert completed.state_id == "state-done"
    assert work_items_harness.audit_log.entries_for_action("work_item.completed")


@pytest.mark.asyncio
async def test_mark_complete_no_completed_state_in_project(
    work_items_harness: WorkItemsHarness,
) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )
    work_items_harness.prom_api_client.states_by_project["proj-1"] = [
        WorkItemState(id="state-backlog", name="Backlog", group="backlog"),
    ]
    work_item, _ = await work_items_harness.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        title="Fix the bug",
        correlation_id="corr-1",
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - asserting error_code below anyway
        await work_items_harness.mark_complete.execute(
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER_ID,
            workspace_slug="acme",
            project_id="proj-1",
            work_item_id=work_item.id,
            correlation_id="corr-2",
        )
    assert exc_info.value.error_code == "PROM_REQUEST_INVALID"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_search_and_lookups(work_items_harness: WorkItemsHarness) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )
    work_items_harness.prom_api_client.projects = [
        ProjectRef(id="proj-1", name="Acme", identifier="ACM")
    ]
    work_items_harness.prom_api_client.members_by_project["proj-1"] = [
        ProjectMemberRef(user_id=PROM_USER_ID, display_name="Test User")
    ]

    projects = await work_items_harness.list_projects.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        correlation_id="corr-1",
    )
    assert [p.id for p in projects] == ["proj-1"]

    members = await work_items_harness.list_project_members.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_id="proj-1",
        correlation_id="corr-2",
    )
    assert [m.user_id for m in members] == [PROM_USER_ID]


@pytest.mark.asyncio
async def test_my_tasks_filters_by_assignee_and_view(work_items_harness: WorkItemsHarness) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )

    today = work_items_harness.clock.now().date()
    mine_due_today = WorkItem(
        id="wi-1",
        sequence_id=1,
        project_id="proj-1",
        name="A",
        state_id=None,
        priority=None,
        assignee_ids=[PROM_USER_ID],
        target_date=today,
        completed_at=None,
    )
    mine_overdue = WorkItem(
        id="wi-2",
        sequence_id=2,
        project_id="proj-1",
        name="B",
        state_id=None,
        priority=None,
        assignee_ids=[PROM_USER_ID],
        target_date=date(2020, 1, 1),
        completed_at=None,
    )
    not_mine = WorkItem(
        id="wi-3",
        sequence_id=3,
        project_id="proj-1",
        name="C",
        state_id=None,
        priority=None,
        assignee_ids=["someone-else"],
        target_date=today,
        completed_at=None,
    )
    work_items_harness.prom_api_client.pages_by_project["proj-1"] = [
        [mine_due_today, mine_overdue, not_mine]
    ]

    assigned = await work_items_harness.list_my_work_items.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_ids=["proj-1"],
        view="assigned",
        correlation_id="corr-1",
    )
    assert {item.id for item in assigned.items} == {"wi-1", "wi-2"}
    assert assigned.truncated is False

    due_today = await work_items_harness.list_my_work_items.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_ids=["proj-1"],
        view="due_today",
        correlation_id="corr-2",
    )
    assert {item.id for item in due_today.items} == {"wi-1"}

    overdue = await work_items_harness.list_my_work_items.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_ids=["proj-1"],
        view="overdue",
        correlation_id="corr-3",
    )
    assert {item.id for item in overdue.items} == {"wi-2"}


@pytest.mark.asyncio
async def test_my_tasks_caps_projects_and_pages(work_items_harness: WorkItemsHarness) -> None:
    customer = await work_items_harness.seed_customer()
    instance = await work_items_harness.seed_prom_instance(customer.id)
    await work_items_harness.link_user(
        customer_id=customer.id, prom_instance_id=instance.id, prom_user_id=PROM_USER_ID
    )

    # 12 distinct projects requested -> only the first 10 (_MAX_PROJECTS_PER_CALL) may be queried.
    project_ids = [f"proj-{i}" for i in range(12)]
    # 5 pages configured per project -> only the first 3 (_MAX_PAGES_PER_PROJECT) may be fetched.
    for project_id in project_ids[:10]:
        work_items_harness.prom_api_client.pages_by_project[project_id] = [[] for _ in range(5)]

    result = await work_items_harness.list_my_work_items.execute(
        prom_instance_id=instance.id,
        prom_user_id=PROM_USER_ID,
        workspace_slug="acme",
        project_ids=project_ids,
        view="assigned",
        correlation_id="corr-1",
    )

    assert result.truncated is True
    queried_projects = {
        project_id for project_id, _cursor in work_items_harness.prom_api_client.list_calls
    }
    assert queried_projects == set(project_ids[:10])
    for project_id in project_ids[:10]:
        pages_fetched = [
            cursor
            for pid, cursor in work_items_harness.prom_api_client.list_calls
            if pid == project_id
        ]
        assert len(pages_fetched) == 3
