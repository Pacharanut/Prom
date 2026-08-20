"""HTTP client for Plane/Prom's public REST API. Implements PromApiClient (get_current_user from
Gate 3, work-item/project/state/member/search methods from Gate 4).

Field names below (project/state/assignees/target_date/etc.) are taken directly from the pinned
`plane` source (serializers + models), not captured from a live HTTP response — DRF's default
ModelSerializer behaviour serializes a FK under the model field's own name (e.g. Issue.project ->
JSON key "project"), which is what this client assumes. Flagged as a residual verification gap:
worth a contract test against a real Plane instance before Gate 5/6 depend on this in production.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx

from src.application.prom_api.ports import PromApiClient, PromUserProfile, WorkItemPage
from src.domain.work_items.entities import (
    ProjectMemberRef,
    ProjectRef,
    WorkItem,
    WorkItemSearchResult,
    WorkItemState,
)
from src.shared.errors import (
    NotFoundError,
    PromApiUnavailableError,
    PromCredentialInvalidError,
    PromPermissionDeniedError,
    PromRequestInvalidError,
    WorkItemNotFoundError,
)

_QueryParams = dict[str, str | int | float | bool | None]
_REQUEST_TIMEOUT_SECONDS = 10.0
_LIST_PAGE_CAP = 100
"""list_projects/list_states/list_project_members fetch only the first page (Plane's default
per_page) — a Phase 1 scope decision, not a full multi-page walk like list_work_items supports.
Documented in README as a known limitation: workspaces with more than 100 projects/states/members
would be truncated."""


def _parse_work_item(data: dict[str, Any], *, fallback_project_id: str | None = None) -> WorkItem:
    """`data` is always the direct result of `response.json()` — untyped JSON, hence `Any` rather
    than `dict[str, object]`: the fields read here are converted defensively (str()/int()) at the
    point of use rather than trusted structurally."""
    target_date_raw = data.get("target_date")
    completed_at_raw = data.get("completed_at")
    project_id = data.get("project") or fallback_project_id
    return WorkItem(
        id=str(data["id"]),
        sequence_id=int(data["sequence_id"]),
        project_id=str(project_id),
        name=str(data["name"]),
        state_id=str(data["state"]) if data.get("state") else None,
        priority=str(data["priority"]) if data.get("priority") else None,
        assignee_ids=[str(a) for a in (data.get("assignees") or [])],
        target_date=date.fromisoformat(str(target_date_raw)) if target_date_raw else None,
        completed_at=(
            datetime.fromisoformat(str(completed_at_raw).replace("Z", "+00:00"))
            if completed_at_raw
            else None
        ),
    )


def _extract_results(body: Any) -> list[dict[str, Any]]:
    """Every "list" endpoint here uses the same paginated envelope (results/next_cursor/
    next_page_results, confirmed against BasePaginator.paginate) — but falls back to treating the
    body as a bare list if a future/other-edition endpoint ever isn't paginated, rather than
    crashing on an unexpected shape."""
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        results: list[dict[str, Any]] = body["results"]
        return results
    if isinstance(body, list):
        return body
    return []


class HttpPromApiClient(PromApiClient):
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        """`client` is injectable for tests (httpx.AsyncClient(transport=httpx.MockTransport(...
        ))), matching src/channels/line/validator.py's pattern — defaults to a real client
        created per-request when not provided."""
        self._injected_client = client

    async def _request(
        self,
        method: str,
        base_url: str,
        token: str,
        path: str,
        *,
        params: _QueryParams | None = None,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        url = f"{base_url.rstrip('/')}/api/v1/{path}"
        owns_client = self._injected_client is None
        http_client = self._injected_client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
        try:
            try:
                return await http_client.request(
                    method, url, headers={"X-Api-Key": token}, params=params, json=json_body
                )
            except httpx.HTTPError as exc:
                raise PromApiUnavailableError(f"prom api request failed: {exc}") from exc
        finally:
            if owns_client:
                await http_client.aclose()

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, *, not_found_error: type[Exception] = NotFoundError
    ) -> None:
        if response.status_code in (401, 403):
            raise PromPermissionDeniedError(
                f"prom api denied the request (HTTP {response.status_code})"
            )
        if response.status_code == 404:
            raise not_found_error(f"prom api returned 404 for {response.request.url}")
        if response.status_code == 400:
            raise PromRequestInvalidError(f"prom api rejected the request: {response.text}")
        if response.status_code >= 400:
            raise PromApiUnavailableError(
                f"prom api returned an unexpected status (HTTP {response.status_code})"
            )

    async def get_current_user(self, base_url: str, token: str) -> PromUserProfile:
        response = await self._request("GET", base_url, token, "users/me/")
        if response.status_code in (401, 403):
            raise PromCredentialInvalidError(
                f"prom api rejected the delegated token (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise PromApiUnavailableError(
                f"prom api returned an unexpected status (HTTP {response.status_code})"
            )
        body = response.json()
        return PromUserProfile(
            id=str(body["id"]),
            email=str(body["email"]),
            display_name=str(body.get("display_name") or body.get("first_name") or body["email"]),
        )

    async def list_projects(
        self, base_url: str, token: str, workspace_slug: str
    ) -> list[ProjectRef]:
        response = await self._request(
            "GET",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/",
            params={"per_page": _LIST_PAGE_CAP},
        )
        self._raise_for_status(response)
        return [
            ProjectRef(
                id=str(p["id"]), name=str(p["name"]), identifier=str(p.get("identifier", ""))
            )
            for p in _extract_results(response.json())
        ]

    async def list_states(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[WorkItemState]:
        response = await self._request(
            "GET",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/states/",
            params={"per_page": _LIST_PAGE_CAP},
        )
        self._raise_for_status(response)
        return [
            WorkItemState(id=str(s["id"]), name=str(s["name"]), group=str(s["group"]))
            for s in _extract_results(response.json())
        ]

    async def list_project_members(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[ProjectMemberRef]:
        response = await self._request(
            "GET",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/project-members-lite/",
            params={"per_page": _LIST_PAGE_CAP},
        )
        self._raise_for_status(response)
        return [
            ProjectMemberRef(user_id=str(m["id"]), display_name=str(m["display_name"]))
            for m in _extract_results(response.json())
        ]

    async def create_work_item(
        self,
        base_url: str,
        token: str,
        workspace_slug: str,
        project_id: str,
        *,
        name: str,
        state_id: str | None,
        assignee_ids: list[str] | None,
        target_date: date | None,
        description_html: str | None,
        external_id: str,
        external_source: str,
    ) -> tuple[WorkItem, bool]:
        body: dict[str, object] = {
            "name": name,
            "external_id": external_id,
            "external_source": external_source,
        }
        if state_id is not None:
            body["state"] = state_id
        if assignee_ids:
            body["assignees"] = assignee_ids
        if target_date is not None:
            body["target_date"] = target_date.isoformat()
        if description_html is not None:
            body["description_html"] = description_html

        response = await self._request(
            "POST",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/work-items/",
            json_body=body,
        )
        if response.status_code == 409:
            existing_id = response.json().get("id")
            if not existing_id:
                raise PromApiUnavailableError(
                    "prom api returned 409 (external_id conflict) without an existing work item id"
                )
            item = await self.get_work_item(
                base_url, token, workspace_slug, project_id, str(existing_id)
            )
            return item, True

        self._raise_for_status(response)
        if response.status_code != 201:
            raise PromApiUnavailableError(
                f"unexpected status creating work item (HTTP {response.status_code})"
            )
        return _parse_work_item(response.json(), fallback_project_id=project_id), False

    async def get_work_item(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> WorkItem:
        response = await self._request(
            "GET",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/",
        )
        self._raise_for_status(response, not_found_error=WorkItemNotFoundError)
        return _parse_work_item(response.json(), fallback_project_id=project_id)

    async def list_work_items(
        self,
        base_url: str,
        token: str,
        workspace_slug: str,
        project_id: str,
        *,
        cursor: str | None = None,
        per_page: int = 100,
    ) -> WorkItemPage:
        params: _QueryParams = {"per_page": per_page}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self._request(
            "GET",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/work-items/",
            params=params,
        )
        self._raise_for_status(response)
        body = response.json()
        items = [
            _parse_work_item(item, fallback_project_id=project_id)
            for item in _extract_results(body)
        ]
        next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
        has_more = bool(body.get("next_page_results")) if isinstance(body, dict) else False
        return WorkItemPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def update_work_item(
        self,
        base_url: str,
        token: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        *,
        fields: dict[str, object],
    ) -> WorkItem:
        response = await self._request(
            "PATCH",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/",
            json_body=fields,
        )
        self._raise_for_status(response, not_found_error=WorkItemNotFoundError)
        return _parse_work_item(response.json(), fallback_project_id=project_id)

    async def add_comment(
        self,
        base_url: str,
        token: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        *,
        comment_html: str,
    ) -> str:
        response = await self._request(
            "POST",
            base_url,
            token,
            f"workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/comments/",
            json_body={"comment_html": comment_html},
        )
        self._raise_for_status(response, not_found_error=WorkItemNotFoundError)
        return str(response.json()["id"])

    async def search_work_items(
        self,
        base_url: str,
        token: str,
        workspace_slug: str,
        *,
        query: str,
        limit: int = 10,
        workspace_search: bool = False,
        project_id: str | None = None,
    ) -> list[WorkItemSearchResult]:
        params: _QueryParams = {
            "search": query,
            "limit": limit,
            "workspace_search": "true" if workspace_search else "false",
        }
        if project_id is not None:
            params["project_id"] = project_id
        response = await self._request(
            "GET", base_url, token, f"workspaces/{workspace_slug}/work-items/search/", params=params
        )
        self._raise_for_status(response)
        body = response.json()
        rows = body.get("issues", []) if isinstance(body, dict) else []
        return [
            WorkItemSearchResult(
                id=str(row["id"]),
                name=str(row["name"]),
                sequence_id=int(row["sequence_id"]),
                project_id=str(row["project_id"]),
                project_identifier=str(row["project__identifier"]),
            )
            for row in rows
        ]
