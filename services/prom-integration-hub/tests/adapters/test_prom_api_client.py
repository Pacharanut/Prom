"""HttpPromApiClient tests using httpx.MockTransport (no real network call) — covers the new
Gate 4 work-item methods and the error-code mapping shared by all of them. get_current_user's
own dedicated test predates this file (Gate 3); not duplicated here.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.adapters.prom_api.client import HttpPromApiClient
from src.shared.errors import (
    PromApiUnavailableError,
    PromPermissionDeniedError,
    PromRequestInvalidError,
    WorkItemNotFoundError,
)

_BASE_URL = "https://prom.acme.example"
_TOKEN = "tok-123"


def _client(handler_fn: Callable[[httpx.Request], httpx.Response]) -> HttpPromApiClient:
    transport = httpx.MockTransport(handler_fn)
    return HttpPromApiClient(client=httpx.AsyncClient(transport=transport))


@pytest.mark.asyncio
async def test_create_work_item_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/workspaces/acme/projects/proj-1/work-items/"
        assert request.headers["X-Api-Key"] == _TOKEN
        return httpx.Response(
            201,
            json={
                "id": "wi-1",
                "sequence_id": 1,
                "project": "proj-1",
                "name": "Fix it",
                "state": "state-1",
                "priority": "high",
                "assignees": ["user-1"],
                "target_date": "2026-02-01",
                "completed_at": None,
            },
        )

    client = _client(handler)
    item, was_replay = await client.create_work_item(
        _BASE_URL,
        _TOKEN,
        "acme",
        "proj-1",
        name="Fix it",
        state_id="state-1",
        assignee_ids=["user-1"],
        target_date=None,
        description_html=None,
        external_id="ext-1",
        external_source="prom-integration-hub",
    )

    assert was_replay is False
    assert item.id == "wi-1"
    assert item.name == "Fix it"
    assert item.assignee_ids == ["user-1"]


@pytest.mark.asyncio
async def test_create_work_item_409_triggers_replay_refetch() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(409, json={"error": "duplicate", "id": "wi-existing"})
        return httpx.Response(
            200,
            json={
                "id": "wi-existing",
                "sequence_id": 5,
                "project": "proj-1",
                "name": "Already there",
                "state": None,
                "priority": None,
                "assignees": [],
                "target_date": None,
                "completed_at": None,
            },
        )

    client = _client(handler)
    item, was_replay = await client.create_work_item(
        _BASE_URL,
        _TOKEN,
        "acme",
        "proj-1",
        name="Fix it",
        state_id=None,
        assignee_ids=None,
        target_date=None,
        description_html=None,
        external_id="ext-1",
        external_source="prom-integration-hub",
    )

    assert was_replay is True
    assert item.id == "wi-existing"
    assert calls == ["POST", "GET"]


@pytest.mark.asyncio
async def test_list_work_items_parses_pagination_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "wi-1",
                        "sequence_id": 1,
                        "project": "proj-1",
                        "name": "A",
                        "state": None,
                        "priority": None,
                        "assignees": [],
                        "target_date": None,
                        "completed_at": None,
                    }
                ],
                "next_cursor": "100:1:0",
                "next_page_results": True,
                "prev_cursor": "100:0:0",
                "prev_page_results": False,
                "count": 1,
                "total_count": 2,
                "total_pages": 2,
            },
        )

    client = _client(handler)
    page = await client.list_work_items(_BASE_URL, _TOKEN, "acme", "proj-1")

    assert len(page.items) == 1
    assert page.items[0].id == "wi-1"
    assert page.has_more is True
    assert page.next_cursor == "100:1:0"


@pytest.mark.asyncio
async def test_search_work_items_parses_issues_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/workspaces/acme/work-items/search/"
        return httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "id": "wi-1",
                        "name": "Fix it",
                        "sequence_id": "1",
                        "project__identifier": "ACM",
                        "project_id": "proj-1",
                        "workspace__slug": "acme",
                    }
                ]
            },
        )

    client = _client(handler)
    results = await client.search_work_items(_BASE_URL, _TOKEN, "acme", query="fix")

    assert len(results) == 1
    assert results[0].id == "wi-1"
    assert results[0].project_identifier == "ACM"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, PromPermissionDeniedError),
        (403, PromPermissionDeniedError),
        (400, PromRequestInvalidError),
        (500, PromApiUnavailableError),
    ],
)
async def test_error_mapping_for_list_states(
    status_code: int, expected_error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "nope"})

    client = _client(handler)
    with pytest.raises(expected_error):
        await client.list_states(_BASE_URL, _TOKEN, "acme", "proj-1")


@pytest.mark.asyncio
async def test_get_work_item_404_maps_to_work_item_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = _client(handler)
    with pytest.raises(WorkItemNotFoundError):
        await client.get_work_item(_BASE_URL, _TOKEN, "acme", "proj-1", "wi-missing")


@pytest.mark.asyncio
async def test_add_comment_returns_comment_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(201, json={"id": "comment-1", "comment_html": "<p>hi</p>"})

    client = _client(handler)
    comment_id = await client.add_comment(
        _BASE_URL, _TOKEN, "acme", "proj-1", "wi-1", comment_html="<p>hi</p>"
    )
    assert comment_id == "comment-1"
