"""Structural assertions on LINE message builders — right message type, right postback data
present — not pixel-perfect JSON snapshots.
"""

from __future__ import annotations

from src.application.prom_api.ports import PromUserProfile
from src.channels.line import messages as msg
from src.channels.line.postback import decode_postback
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem, WorkItemState


def _postback_datas(message: dict[str, object]) -> list[str]:
    quick_reply = message.get("quickReply")
    assert isinstance(quick_reply, dict)
    items = quick_reply["items"]
    assert isinstance(items, list)
    return [item["action"]["data"] for item in items]  # type: ignore[index]


def test_menu_message_has_three_options() -> None:
    message = msg.build_menu_message()
    assert message["type"] == "text"
    datas = _postback_datas(message)
    actions = {decode_postback(d)["action"] for d in datas}
    assert actions == {"create", "my_tasks", "update"}


def test_project_picker_encodes_project_ids() -> None:
    projects = [ProjectRef(id="p1", name="Engineering", identifier="ENG")]
    message = msg.build_project_picker(projects)
    fields = decode_postback(_postback_datas(message)[0])
    assert fields == {"action": "select_project", "value": "p1"}


def test_state_picker_includes_skip_option() -> None:
    states = [WorkItemState(id="s1", name="Todo", group="unstarted")]
    message = msg.build_state_picker(states)
    datas = [decode_postback(d) for d in _postback_datas(message)]
    assert {"action": "select_state", "value": "skip"} in datas
    assert {"action": "select_state", "value": "s1"} in datas


def test_assignee_picker_includes_skip_option() -> None:
    members = [ProjectMemberRef(user_id="m1", display_name="Alice")]
    message = msg.build_assignee_picker(members)
    datas = [decode_postback(d) for d in _postback_datas(message)]
    assert {"action": "select_assignee", "value": "skip"} in datas
    assert {"action": "select_assignee", "value": "m1"} in datas


def test_review_card_includes_confirm_and_cancel() -> None:
    message = msg.build_review_card(
        title="Fix bug",
        project_name="Eng",
        state_name="Todo",
        assignee_name="Alice",
        due_date="2026-02-01",
    )
    actions = {decode_postback(d)["action"] for d in _postback_datas(message)}
    assert actions == {"confirm", "cancel"}
    assert "Fix bug" in message["text"]  # type: ignore[operator]


def test_create_success_message_includes_identifier_and_link() -> None:
    work_item = WorkItem(
        id="wi-1",
        sequence_id=42,
        project_id="p1",
        name="Fix bug",
        state_id=None,
        priority=None,
        assignee_ids=[],
        target_date=None,
        completed_at=None,
    )
    message = msg.build_create_success_message(
        work_item=work_item,
        project_identifier="ENG",
        web_url="https://prom.example/acme/projects/p1/issues/wi-1",
        was_replay=False,
    )
    text = message["text"]
    assert isinstance(text, str)
    assert "ENG-42" in text
    assert "https://prom.example/acme/projects/p1/issues/wi-1" in text


def test_error_message_maps_known_codes() -> None:
    message = msg.build_error_message("LINKING_CODE_EXPIRED")
    assert "หมดอายุ" in message["text"]  # type: ignore[operator]


def test_error_message_falls_back_for_unknown_codes() -> None:
    message = msg.build_error_message("SOME_UNKNOWN_CODE")
    assert message["type"] == "text"


def test_my_tasks_list_encodes_work_item_and_project() -> None:
    item = WorkItem(
        id="wi-9",
        sequence_id=7,
        project_id="p1",
        name="Task",
        state_id=None,
        priority=None,
        assignee_ids=["u1"],
        target_date=None,
        completed_at=None,
    )
    projects_by_id = {"p1": ProjectRef(id="p1", name="Eng", identifier="ENG")}
    message = msg.build_my_tasks_list([item], projects_by_id, truncated=False)
    fields = decode_postback(_postback_datas(message)[0])
    assert fields == {"action": "select_work_item", "value": "wi-9", "project": "p1"}
    assert "ENG-7" in message["text"]  # type: ignore[operator]


def test_my_tasks_list_empty() -> None:
    message = msg.build_my_tasks_list([], {}, truncated=False)
    assert "quickReply" not in message


def test_update_action_menu_has_four_actions_plus_cancel() -> None:
    message = msg.build_update_action_menu()
    decoded = [decode_postback(d) for d in _postback_datas(message)]
    assert {f["action"] for f in decoded} == {"select_update_action", "cancel"}
    values = {f["value"] for f in decoded if f["action"] == "select_update_action"}
    assert values == {"state", "comment", "assignee", "complete"}


def test_linking_success_message_includes_display_name() -> None:
    profile = PromUserProfile(id="u1", email="alice@example.com", display_name="Alice")
    message = msg.build_linking_success_message(profile, has_multiple_workspaces=False)
    assert "Alice" in message["text"]  # type: ignore[operator]


def test_linking_success_message_notes_multiple_workspaces() -> None:
    profile = PromUserProfile(id="u1", email="alice@example.com", display_name="Alice")
    message = msg.build_linking_success_message(profile, has_multiple_workspaces=True)
    assert "Workspace" in message["text"]  # type: ignore[operator]
