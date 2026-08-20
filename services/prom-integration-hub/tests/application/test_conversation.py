"""Unit tests for the channel-agnostic CreateWorkItemFlow / UpdateWorkItemFlow state machines —
no LINE/Discord involved, fakes only. These are what Gate 6 (Discord) will reuse verbatim, so
correctness here matters more than in a channel-specific test.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.application.work_items.conversation import (
    CreateFlowStep,
    CreateWorkItemFlow,
    FlowContext,
    InvalidConversationInputError,
    UpdateFlowStep,
    UpdateWorkItemFlow,
)
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    ListProjectMembers,
    ListProjects,
    ListStates,
    MarkComplete,
    UpdateWorkItemState,
)
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItemState
from tests.application.conftest import WorkItemsHarness


def _ports(h: WorkItemsHarness) -> dict[str, object]:
    return {
        "prom_instances": h.prom_instances,
        "delegated_credentials": h.delegated_credentials,
        "secrets_provider": h.secrets_provider,
        "prom_api_client": h.prom_api_client,
        "audit_log": h.audit_log,
        "clock": h.clock,
    }


def _create_flow(h: WorkItemsHarness) -> CreateWorkItemFlow:
    p = _ports(h)
    return CreateWorkItemFlow(
        list_projects=ListProjects(**p),  # type: ignore[arg-type]
        list_states=ListStates(**p),  # type: ignore[arg-type]
        list_project_members=ListProjectMembers(**p),  # type: ignore[arg-type]
        create_work_item=CreateWorkItem(**p),  # type: ignore[arg-type]
    )


def _update_flow(h: WorkItemsHarness) -> UpdateWorkItemFlow:
    p = _ports(h)
    return UpdateWorkItemFlow(
        list_states=ListStates(**p),  # type: ignore[arg-type]
        list_project_members=ListProjectMembers(**p),  # type: ignore[arg-type]
        update_work_item_state=UpdateWorkItemState(**p),  # type: ignore[arg-type]
        change_assignee=ChangeAssignee(**p),  # type: ignore[arg-type]
        add_comment=AddComment(**p),  # type: ignore[arg-type]
        mark_complete=MarkComplete(**p),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_create_flow_happy_path_end_to_end(work_items_harness: WorkItemsHarness) -> None:
    h = work_items_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    await h.link_user(customer_id=customer.id, prom_instance_id=instance.id, prom_user_id="u1")

    h.prom_api_client.projects = [ProjectRef(id="p1", name="Engineering", identifier="ENG")]
    h.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="s1", name="Todo", group="unstarted")
    ]
    h.prom_api_client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="m1", display_name="Alice")
    ]

    flow = _create_flow(h)
    ctx = FlowContext(
        prom_instance_id=instance.id, prom_user_id="u1", workspace_slug="acme", correlation_id="c1"
    )

    state = flow.start()
    assert state.step == CreateFlowStep.TITLE

    state, projects = await flow.submit_title(ctx, state, "Fix the login bug")
    assert state.step == CreateFlowStep.PROJECT
    assert [p.id for p in projects] == ["p1"]

    state, states = await flow.submit_project(ctx, state, "p1")
    assert state.step == CreateFlowStep.STATE
    assert [s.id for s in states] == ["s1"]

    state, members = await flow.submit_state(ctx, state, "s1")
    assert state.step == CreateFlowStep.ASSIGNEE
    assert [m.user_id for m in members] == ["m1"]

    state = flow.submit_assignee(state, "m1")
    assert state.step == CreateFlowStep.DUE_DATE

    state = flow.submit_due_date_text(state, "2026-02-01")
    assert state.step == CreateFlowStep.REVIEW
    assert state.data["due_date"] == "2026-02-01"

    work_item, was_replay = await flow.confirm(ctx, state, idempotency_key="key-1")
    assert was_replay is False
    assert work_item.name == "Fix the login bug"
    assert work_item.assignee_ids == ["m1"]
    assert work_item.state_id == "s1"


@pytest.mark.asyncio
async def test_create_flow_skip_assignee_and_due_date(work_items_harness: WorkItemsHarness) -> None:
    h = work_items_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    await h.link_user(customer_id=customer.id, prom_instance_id=instance.id, prom_user_id="u1")
    h.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]

    flow = _create_flow(h)
    ctx = FlowContext(
        prom_instance_id=instance.id, prom_user_id="u1", workspace_slug="acme", correlation_id="c1"
    )
    state = flow.start()
    state, _ = await flow.submit_title(ctx, state, "Some task")
    state, _ = await flow.submit_project(ctx, state, "p1")
    state, _ = await flow.submit_state(ctx, state, None)  # skip state
    state = flow.submit_assignee(state, None)  # skip assignee
    state = flow.submit_due_date_text(state, "skip")  # skip due date
    assert state.data["due_date"] is None

    work_item, _ = await flow.confirm(ctx, state, idempotency_key="key-2")
    assert work_item.assignee_ids == []
    assert work_item.state_id is None
    assert work_item.target_date is None


@pytest.mark.asyncio
async def test_empty_title_is_rejected(work_items_harness: WorkItemsHarness) -> None:
    h = work_items_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    flow = _create_flow(h)
    ctx = FlowContext(
        prom_instance_id=instance.id, prom_user_id="u1", workspace_slug="acme", correlation_id="c1"
    )
    state = flow.start()
    with pytest.raises(InvalidConversationInputError):
        await flow.submit_title(ctx, state, "   ")


def test_invalid_due_date_text_is_rejected(work_items_harness: WorkItemsHarness) -> None:
    flow = _create_flow(work_items_harness)
    state = flow.start()
    state.step = CreateFlowStep.DUE_DATE
    state.data = {"title": "x", "project_id": "p1"}
    with pytest.raises(InvalidConversationInputError):
        flow.submit_due_date_text(state, "not-a-date")


@pytest.mark.asyncio
async def test_create_flow_idempotent_confirm_replay(work_items_harness: WorkItemsHarness) -> None:
    """Two independent `confirm()` calls with the SAME idempotency_key must not create two work
    items — this is CreateWorkItem's own dedup (Gate 4), exercised here through the flow's public
    surface to prove the flow actually wires idempotency_key through end to end."""
    h = work_items_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    await h.link_user(customer_id=customer.id, prom_instance_id=instance.id, prom_user_id="u1")
    h.prom_api_client.projects = [ProjectRef(id="p1", name="Eng", identifier="ENG")]

    flow = _create_flow(h)
    ctx = FlowContext(
        prom_instance_id=instance.id, prom_user_id="u1", workspace_slug="acme", correlation_id="c1"
    )
    state = flow.start()
    state, _ = await flow.submit_title(ctx, state, "Duplicate-prone task")
    state, _ = await flow.submit_project(ctx, state, "p1")
    state, _ = await flow.submit_state(ctx, state, None)
    state = flow.submit_assignee(state, None)
    state = flow.submit_due_date_text(state, "skip")

    first, first_replay = await flow.confirm(ctx, state, idempotency_key="stable-key")
    second, second_replay = await flow.confirm(ctx, state, idempotency_key="stable-key")

    assert first_replay is False
    assert second_replay is True
    assert first.id == second.id


@pytest.mark.asyncio
async def test_update_flow_mark_complete_requires_confirmation(
    work_items_harness: WorkItemsHarness,
) -> None:
    h = work_items_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    await h.link_user(customer_id=customer.id, prom_instance_id=instance.id, prom_user_id="u1")
    h.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="done", name="Done", group="completed")
    ]
    work_item, _ = await h.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id="u1",
        workspace_slug="acme",
        project_id="p1",
        title="Existing",
        state_id=None,
        assignee_ids=None,
        due_date=None,
        idempotency_key="seed",
        correlation_id="seed",
    )

    flow = _update_flow(h)
    ctx = FlowContext(
        prom_instance_id=instance.id, prom_user_id="u1", workspace_slug="acme", correlation_id="c1"
    )
    state = flow.start_for_work_item("p1", work_item.id)
    assert state.step == UpdateFlowStep.SELECT_ACTION

    confirm_state = flow.select_mark_complete(state)
    assert confirm_state.step == UpdateFlowStep.CONFIRM_COMPLETE
    # The use case must NOT have run yet just from selecting the action.
    assert work_item.completed_at is None

    completed = await flow.confirm_mark_complete(ctx, confirm_state)
    assert completed.state_id == "done"


@pytest.mark.asyncio
async def test_update_flow_state_change_and_assignee_and_comment(
    work_items_harness: WorkItemsHarness,
) -> None:
    h = work_items_harness
    customer = await h.seed_customer()
    instance = await h.seed_prom_instance(customer.id)
    await h.link_user(customer_id=customer.id, prom_instance_id=instance.id, prom_user_id="u1")
    h.prom_api_client.states_by_project["p1"] = [
        WorkItemState(id="s2", name="In Progress", group="started")
    ]
    h.prom_api_client.members_by_project["p1"] = [
        ProjectMemberRef(user_id="m2", display_name="Bob")
    ]
    work_item, _ = await h.create_work_item.execute(
        prom_instance_id=instance.id,
        prom_user_id="u1",
        workspace_slug="acme",
        project_id="p1",
        title="Existing 2",
        state_id=None,
        assignee_ids=None,
        due_date=None,
        idempotency_key=str(uuid4()),
        correlation_id="seed2",
    )

    flow = _update_flow(h)
    ctx = FlowContext(
        prom_instance_id=instance.id, prom_user_id="u1", workspace_slug="acme", correlation_id="c1"
    )
    state = flow.start_for_work_item("p1", work_item.id)

    state, states = await flow.select_change_state(ctx, state)
    assert [s.id for s in states] == ["s2"]
    updated = await flow.confirm_state_change(ctx, state, "s2")
    assert updated.state_id == "s2"

    state = flow.start_for_work_item("p1", work_item.id)
    state, members = await flow.select_reassign(ctx, state)
    assert [m.user_id for m in members] == ["m2"]
    reassigned = await flow.confirm_reassign(ctx, state, "m2")
    assert reassigned.assignee_ids == ["m2"]

    state = flow.start_for_work_item("p1", work_item.id)
    comment_state = flow.select_add_comment(state)
    assert comment_state.step == UpdateFlowStep.AWAIT_COMMENT
    comment_id = await flow.confirm_add_comment(ctx, comment_state, "looks good")
    assert comment_id
