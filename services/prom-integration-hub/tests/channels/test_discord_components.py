"""Structural assertions for src/channels/discord/components.py's DiscordMessage builders —
right component types, right custom_ids, option/button counts within Discord's real limits.
Mirrors tests/channels/test_line_messages.py's intent (if present) for the LINE side.
"""

from __future__ import annotations

from datetime import date

from src.application.prom_api.ports import PromUserProfile
from src.channels.discord import components as comp
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem, WorkItemState
from src.domain.workspaces.entities import WorkspaceConnection


def test_menu_message_has_five_buttons() -> None:
    message = comp.build_menu_message()
    assert {b.custom_id for b in message.buttons} == {
        "menu_create",
        "menu_my_tasks",
        "menu_update",
        "menu_summary",
        "menu_notif_settings",
    }
    assert message.select is None
    assert all(len(b.label) <= 80 for b in message.buttons)


def test_project_picker_is_a_single_select() -> None:
    projects = [ProjectRef(id=f"p{i}", name=f"Project {i}", identifier="P") for i in range(3)]
    message = comp.build_project_picker(projects)
    assert message.select is not None
    assert message.select.custom_id == "select_project"
    assert [o.value for o in message.select.options] == ["p0", "p1", "p2"]
    assert not message.buttons


def test_project_picker_caps_at_25_options() -> None:
    projects = [ProjectRef(id=f"p{i}", name=f"Project {i}", identifier="P") for i in range(40)]
    message = comp.build_project_picker(projects)
    assert message.select is not None
    assert len(message.select.options) == 25


def test_state_picker_includes_skip_option_first() -> None:
    states = [WorkItemState(id="s1", name="Todo", group="unstarted")]
    message = comp.build_state_picker(states)
    assert message.select is not None
    assert message.select.options[0].value == "skip"
    assert message.select.options[1].value == "s1"


def test_assignee_picker_includes_skip_option_first() -> None:
    members = [ProjectMemberRef(user_id="u1", display_name="Alice")]
    message = comp.build_assignee_picker(members)
    assert message.select is not None
    assert message.select.options[0].value == "skip"


def test_review_card_has_confirm_and_cancel_buttons() -> None:
    message = comp.build_review_card(
        title="Fix bug",
        project_name="Engineering",
        state_name="Todo",
        assignee_name="Alice",
        due_date="2026-03-01",
    )
    assert {b.custom_id for b in message.buttons} == {"confirm", "cancel"}
    assert "Fix bug" in message.content
    assert message.select is None


def test_confirm_complete_prompt_has_confirm_and_cancel_buttons() -> None:
    message = comp.build_confirm_complete_prompt()
    assert {b.custom_id for b in message.buttons} == {"confirm", "cancel"}


def test_update_action_menu_is_a_select_plus_cancel_button() -> None:
    message = comp.build_update_action_menu()
    assert message.select is not None
    assert message.select.custom_id == "select_update_action"
    assert {o.value for o in message.select.options} == {
        "state",
        "comment",
        "assignee",
        "complete",
        "history",
    }
    assert {b.custom_id for b in message.buttons} == {"cancel"}


def test_workspace_picker_uses_workspace_ids_as_values() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    now = datetime(2026, 1, 1, tzinfo=UTC)
    ws = WorkspaceConnection(
        id=uuid4(),
        customer_id=uuid4(),
        prom_instance_id=uuid4(),
        prom_workspace_id="w1",
        prom_workspace_slug="acme",
        name="Acme",
        created_at=now,
        updated_at=now,
    )
    message = comp.build_workspace_picker([ws])
    assert message.select is not None
    assert message.select.options[0].value == str(ws.id)


def test_my_tasks_list_encodes_project_and_work_item_id_together() -> None:
    item = WorkItem(
        id="wi1",
        sequence_id=42,
        project_id="p1",
        name="Fix bug",
        state_id=None,
        priority=None,
        assignee_ids=["u1"],
        target_date=None,
        completed_at=None,
    )
    message = comp.build_my_tasks_list(
        [item], {"p1": ProjectRef(id="p1", name="Eng", identifier="ENG")}, truncated=False
    )
    assert message.select is not None
    assert message.select.custom_id == "select_work_item"
    assert message.select.options[0].value == "p1:wi1"
    assert "ENG-42" in message.content


def test_my_tasks_list_empty_has_no_select() -> None:
    message = comp.build_my_tasks_list([], {}, truncated=False)
    assert message.select is None
    assert not message.buttons


def test_error_message_maps_known_code() -> None:
    message = comp.build_error_message("LINKING_CODE_EXPIRED")
    assert "หมดอายุ" in message.content


def test_error_message_falls_back_for_unknown_code() -> None:
    message = comp.build_error_message("SOME_UNKNOWN_CODE")
    assert "เกิดข้อผิดพลาด" in message.content


def test_linking_success_message_mentions_display_name_and_email() -> None:
    profile = PromUserProfile(id="u1", email="alice@example.com", display_name="Alice")
    message = comp.build_linking_success_message(profile, has_multiple_workspaces=False)
    assert "Alice" in message.content
    assert "alice@example.com" in message.content


def test_create_success_message_shows_identifier_and_url() -> None:
    work_item = WorkItem(
        id="wi1",
        sequence_id=7,
        project_id="p1",
        name="Fix bug",
        state_id=None,
        priority=None,
        assignee_ids=[],
        target_date=date(2026, 3, 1),
        completed_at=None,
    )
    message = comp.build_create_success_message(
        work_item=work_item,
        project_identifier="ENG",
        web_url="https://prom.example/acme/projects/p1/issues/wi1",
        was_replay=False,
    )
    assert "ENG-7" in message.content
    assert "https://prom.example/acme/projects/p1/issues/wi1" in message.content
