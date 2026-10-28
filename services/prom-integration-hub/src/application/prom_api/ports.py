"""PromApiClient port — the one and only way any use case talks to Prom/Plane's public REST
API. Framework-free (stdlib typing + dataclasses only); `src/adapters/prom_api/client.py`
implements this with `httpx`.

Every path below was verified against the pinned `plane` upstream source (Gate 0/4 discovery),
not guessed:
  - `apps/api/plane/api/urls/{project,state,member,work_item}.py` for routes
  - `apps/api/plane/api/serializers/issue.py` + `apps/api/plane/db/models/issue.py` for fields
  - `apps/api/plane/utils/paginator.py:BasePaginator.paginate` for the pagination envelope
All routes are relative to `{base_url}/api/v1/workspaces/{workspace_slug}/...`, authenticated via
`X-Api-Key: {token}`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from src.domain.work_items.entities import (
    ProjectMemberRef,
    ProjectRef,
    WorkItem,
    WorkItemActivity,
    WorkItemLabel,
    WorkItemSearchResult,
    WorkItemState,
)


@dataclass
class PromUserProfile:
    id: str
    email: str
    display_name: str


@dataclass
class WorkItemPage:
    """Pagination envelope — not a domain concept itself, just this port's return shape for
    list_work_items. Mirrors the fields ListMyWorkItems actually needs from Plane's real
    pagination response (`next_cursor`, `next_page_results`) without exposing Plane's full,
    much larger envelope (grouped_by, total_pages, extra_stats, ...)."""

    items: list[WorkItem]
    next_cursor: str | None
    has_more: bool


class PromApiClient(Protocol):
    async def get_current_user(self, base_url: str, token: str) -> PromUserProfile:
        """GET {base_url}/api/v1/users/me/. Raises PromApiUnavailableError on network/5xx,
        PromCredentialInvalidError on 401/403."""
        ...

    async def list_projects(
        self, base_url: str, token: str, workspace_slug: str
    ) -> list[ProjectRef]:
        """GET workspaces/{slug}/projects/"""
        ...

    async def list_states(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[WorkItemState]:
        """GET workspaces/{slug}/projects/{project_id}/states/"""
        ...

    async def list_project_members(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[ProjectMemberRef]:
        """GET workspaces/{slug}/projects/{project_id}/project-members-lite/"""
        ...

    async def list_work_item_activities(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> list[WorkItemActivity]:
        """GET workspaces/{slug}/projects/{project_id}/work-items/{id}/activities/ — newest
        first, first page only (100). Phase 2E (timeline)."""
        ...

    async def list_labels(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[WorkItemLabel]:
        """GET workspaces/{slug}/projects/{project_id}/labels/ — first page only (same cap as
        list_states). Phase 2C (auto-tagging)."""
        ...

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
        priority: str | None = None,
        label_ids: list[str] | None = None,
    ) -> tuple[WorkItem, bool]:
        """POST workspaces/{slug}/projects/{project_id}/work-items/. `external_id`+
        `external_source` are always sent — Plane's own dedup: a 409 with the existing item's id
        means "already created", not an error (Gate 0/4 finding, `issue.py:466-489`). Returns
        (work_item, was_replay) — was_replay is True when the 409-then-refetch path was taken."""
        ...

    async def get_work_item(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> WorkItem:
        """GET workspaces/{slug}/projects/{project_id}/work-items/{id}/"""
        ...

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
        """GET workspaces/{slug}/projects/{project_id}/work-items/ — paginated. No server-side
        assignee/due-date filter exists on this Plane edition (`pql`/`filters` params are
        rejected with HTTP 400, Gate 0/4 finding); callers needing "my tasks"-style views must
        filter client-side across pages, which is exactly what ListMyWorkItems does, with
        documented caps."""
        ...

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
        """PATCH workspaces/{slug}/projects/{project_id}/work-items/{id}/ — `fields` is sent
        verbatim as the partial-update body (e.g. {"state": state_id} or
        {"assignees": [...]})."""
        ...

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
        """POST workspaces/{slug}/projects/{project_id}/work-items/{id}/comments/. Returns the
        created comment's id."""
        ...

    async def list_blocked_by(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> list[tuple[str, str]]:
        """GET workspaces/{slug}/projects/{project_id}/work-items/{id}/relations/ — the
        response's `blocked_by` list as (project_id, work_item_id) pairs: the work items that
        block this one. Phase 2B (daily summary "blocked" section); shape verified against
        plane/apps/api/plane/api/views/issue.py IssueRelationListCreateAPIEndpoint.get."""
        ...

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
        """GET workspaces/{slug}/work-items/search/ — automatically scoped by Plane to projects
        the token's user is an active member of (`issue.py:2277-2283`), no extra permission
        filtering needed on Hub's side."""
        ...
