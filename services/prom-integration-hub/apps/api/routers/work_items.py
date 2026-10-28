"""Work-item routes: create/get/list("my tasks")/search/update-state/change-assignee/comment/
complete. Every write route (create, update-state, change-assignee, comment, complete) supports
an optional `Idempotency-Key` header — see apps/api/idempotency.py for the two-layer rationale.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from apps.api.dependencies import (
    get_add_comment,
    get_change_assignee,
    get_channel_installation_repository,
    get_create_work_item,
    get_get_work_item,
    get_get_work_item_timeline,
    get_idempotency_repository,
    get_list_my_work_items,
    get_list_project_members,
    get_list_projects,
    get_list_states,
    get_mark_complete,
    get_search_work_items,
    get_update_work_item_state,
    require_service_token,
)
from apps.api.idempotency import check_idempotency, store_idempotency
from apps.api.schemas import (
    AddCommentRequest,
    AddCommentResponse,
    ChangeAssigneeRequest,
    CompleteWorkItemRequest,
    CreateWorkItemRequest,
    CreateWorkItemResponse,
    MyTasksResponse,
    TimelineEntryResponse,
    UpdateStateRequest,
    WorkItemResponse,
    WorkItemSearchResultResponse,
)
from src.adapters.persistence.channel_installation_repository import (
    PostgresChannelInstallationRepository,
)
from src.application.work_items.ports import IdempotencyRepository
from src.application.work_items.timeline import GetWorkItemTimeline
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    GetWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListStates,
    MarkComplete,
    SearchWorkItems,
    UpdateWorkItemState,
)
from src.domain.work_items.entities import WorkItem
from src.infrastructure.observability.logging import get_correlation_id
from src.shared.errors import NotFoundError

router = APIRouter(
    prefix="/api/v1/work-items", tags=["work-items"], dependencies=[Depends(require_service_token)]
)


def _correlation_id() -> str:
    return get_correlation_id() or "unknown"


def _work_item_response(item: WorkItem) -> WorkItemResponse:
    return WorkItemResponse(
        id=item.id,
        sequence_id=item.sequence_id,
        project_id=item.project_id,
        name=item.name,
        state_id=item.state_id,
        priority=item.priority,
        assignee_ids=item.assignee_ids,
        target_date=item.target_date,
        completed_at=item.completed_at,
    )


async def _resolve_customer_id(
    channel_installation_id: UUID, installations: PostgresChannelInstallationRepository
) -> UUID:
    installation = await installations.get(channel_installation_id)
    if installation is None:
        raise NotFoundError(f"channel_installation {channel_installation_id} not found")
    return installation.customer_id


@router.post("", response_model=CreateWorkItemResponse, status_code=201)
async def create_work_item(
    body: CreateWorkItemRequest,
    use_case: Annotated[CreateWorkItem, Depends(get_create_work_item)],
    installations: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
    idempotency_repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateWorkItemResponse:
    customer_id = await _resolve_customer_id(body.channel_installation_id, installations)
    request_body = body.model_dump(mode="json")

    cached = await check_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route="POST /api/v1/work-items",
        idempotency_key=idempotency_key,
        request_body=request_body,
    )
    if cached is not None:
        return CreateWorkItemResponse.model_validate(cached)

    work_item, was_replay = await use_case.execute(
        prom_instance_id=body.prom_instance_id,
        prom_user_id=body.prom_user_id,
        workspace_slug=body.workspace_slug,
        project_id=body.project_id,
        title=body.title,
        state_id=body.state_id,
        assignee_ids=body.assignee_ids,
        due_date=body.due_date,
        description_html=body.description_html,
        idempotency_key=body.idempotency_key,
        correlation_id=_correlation_id(),
    )
    response = CreateWorkItemResponse(
        work_item=_work_item_response(work_item), was_replay=was_replay
    )

    await store_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route="POST /api/v1/work-items",
        idempotency_key=idempotency_key,
        request_body=request_body,
        response_status=201,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/my-tasks", response_model=MyTasksResponse)
async def list_my_work_items(
    use_case: Annotated[ListMyWorkItems, Depends(get_list_my_work_items)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
    project_ids: Annotated[list[str], Query()],
    view: Annotated[Literal["assigned", "due_today", "overdue"], Query()] = "assigned",
) -> MyTasksResponse:
    result = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        project_ids=project_ids,
        view=view,
        correlation_id=_correlation_id(),
    )
    return MyTasksResponse(
        items=[_work_item_response(item) for item in result.items], truncated=result.truncated
    )


@router.get("/search", response_model=list[WorkItemSearchResultResponse])
async def search_work_items(
    use_case: Annotated[SearchWorkItems, Depends(get_search_work_items)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
    query: Annotated[str, Query()],
    limit: Annotated[int, Query()] = 10,
    project_id: Annotated[str | None, Query()] = None,
) -> list[WorkItemSearchResultResponse]:
    results = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        query=query,
        limit=limit,
        workspace_search=project_id is None,
        project_id=project_id,
        correlation_id=_correlation_id(),
    )
    return [
        WorkItemSearchResultResponse(
            id=r.id,
            name=r.name,
            sequence_id=r.sequence_id,
            project_id=r.project_id,
            project_identifier=r.project_identifier,
        )
        for r in results
    ]


@router.patch("/{work_item_id}/state", response_model=WorkItemResponse)
async def update_work_item_state(
    work_item_id: str,
    body: UpdateStateRequest,
    use_case: Annotated[UpdateWorkItemState, Depends(get_update_work_item_state)],
    installations: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
    idempotency_repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkItemResponse:
    customer_id = await _resolve_customer_id(body.channel_installation_id, installations)
    route = "PATCH /api/v1/work-items/{id}/state"
    request_body: dict[str, Any] = {"work_item_id": work_item_id, **body.model_dump(mode="json")}

    cached = await check_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
    )
    if cached is not None:
        return WorkItemResponse.model_validate(cached)

    work_item = await use_case.execute(
        prom_instance_id=body.prom_instance_id,
        prom_user_id=body.prom_user_id,
        workspace_slug=body.workspace_slug,
        project_id=body.project_id,
        work_item_id=work_item_id,
        state_id=body.state_id,
        correlation_id=_correlation_id(),
    )
    response = _work_item_response(work_item)
    await store_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.patch("/{work_item_id}/assignees", response_model=WorkItemResponse)
async def change_assignee(
    work_item_id: str,
    body: ChangeAssigneeRequest,
    use_case: Annotated[ChangeAssignee, Depends(get_change_assignee)],
    installations: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
    idempotency_repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkItemResponse:
    customer_id = await _resolve_customer_id(body.channel_installation_id, installations)
    route = "PATCH /api/v1/work-items/{id}/assignees"
    request_body: dict[str, Any] = {"work_item_id": work_item_id, **body.model_dump(mode="json")}

    cached = await check_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
    )
    if cached is not None:
        return WorkItemResponse.model_validate(cached)

    work_item = await use_case.execute(
        prom_instance_id=body.prom_instance_id,
        prom_user_id=body.prom_user_id,
        workspace_slug=body.workspace_slug,
        project_id=body.project_id,
        work_item_id=work_item_id,
        assignee_ids=body.assignee_ids,
        correlation_id=_correlation_id(),
    )
    response = _work_item_response(work_item)
    await store_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.post("/{work_item_id}/comments", response_model=AddCommentResponse, status_code=201)
async def add_comment(
    work_item_id: str,
    body: AddCommentRequest,
    use_case: Annotated[AddComment, Depends(get_add_comment)],
    installations: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
    idempotency_repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AddCommentResponse:
    customer_id = await _resolve_customer_id(body.channel_installation_id, installations)
    route = "POST /api/v1/work-items/{id}/comments"
    request_body: dict[str, Any] = {"work_item_id": work_item_id, **body.model_dump(mode="json")}

    cached = await check_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
    )
    if cached is not None:
        return AddCommentResponse.model_validate(cached)

    comment_id = await use_case.execute(
        prom_instance_id=body.prom_instance_id,
        prom_user_id=body.prom_user_id,
        workspace_slug=body.workspace_slug,
        project_id=body.project_id,
        work_item_id=work_item_id,
        comment_html=body.comment_html,
        correlation_id=_correlation_id(),
    )
    response = AddCommentResponse(comment_id=comment_id)
    await store_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
        response_status=201,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.post("/{work_item_id}/complete", response_model=WorkItemResponse)
async def complete_work_item(
    work_item_id: str,
    body: CompleteWorkItemRequest,
    use_case: Annotated[MarkComplete, Depends(get_mark_complete)],
    installations: Annotated[
        PostgresChannelInstallationRepository, Depends(get_channel_installation_repository)
    ],
    idempotency_repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkItemResponse:
    customer_id = await _resolve_customer_id(body.channel_installation_id, installations)
    route = "POST /api/v1/work-items/{id}/complete"
    request_body: dict[str, Any] = {"work_item_id": work_item_id, **body.model_dump(mode="json")}

    cached = await check_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
    )
    if cached is not None:
        return WorkItemResponse.model_validate(cached)

    work_item = await use_case.execute(
        prom_instance_id=body.prom_instance_id,
        prom_user_id=body.prom_user_id,
        workspace_slug=body.workspace_slug,
        project_id=body.project_id,
        work_item_id=work_item_id,
        correlation_id=_correlation_id(),
    )
    response = _work_item_response(work_item)
    await store_idempotency(
        repository=idempotency_repo,
        customer_id=customer_id,
        route=route,
        idempotency_key=idempotency_key,
        request_body=request_body,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


# --- Project/state/member lookups (needed by channel adapters for pickers; not in the original
# Gate 4 use-case list but trivial given the ports already exist, see ListProjects's docstring) --


@router.get("/projects", response_model=list[dict[str, str]])
async def list_projects(
    use_case: Annotated[ListProjects, Depends(get_list_projects)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
) -> list[dict[str, str]]:
    projects = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        correlation_id=_correlation_id(),
    )
    return [{"id": p.id, "name": p.name, "identifier": p.identifier} for p in projects]


@router.get("/projects/{project_id}/states", response_model=list[dict[str, str]])
async def list_states(
    project_id: str,
    use_case: Annotated[ListStates, Depends(get_list_states)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
) -> list[dict[str, str]]:
    states = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        project_id=project_id,
        correlation_id=_correlation_id(),
    )
    return [{"id": s.id, "name": s.name, "group": s.group} for s in states]


@router.get("/projects/{project_id}/members", response_model=list[dict[str, str]])
async def list_project_members(
    project_id: str,
    use_case: Annotated[ListProjectMembers, Depends(get_list_project_members)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
) -> list[dict[str, str]]:
    members = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        project_id=project_id,
        correlation_id=_correlation_id(),
    )
    return [{"user_id": m.user_id, "display_name": m.display_name} for m in members]


# --- Catch-all detail route — MUST be registered last among GET routes on this router. Starlette
# matches routes in registration order, and "/{work_item_id}" is a single-segment wildcard that
# would otherwise shadow "/my-tasks", "/search", and "/projects" (each also a single segment
# after the router's prefix) if it came first — it very nearly did during development; keep any
# new single-segment GET route above this one, not below. -----------------------------------


@router.get("/{work_item_id}/timeline", response_model=list[TimelineEntryResponse])
async def get_work_item_timeline(
    work_item_id: str,
    use_case: Annotated[GetWorkItemTimeline, Depends(get_get_work_item_timeline)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
    project_id: Annotated[str, Query()],
    source: Annotated[Literal["line", "discord", "hub", "plane"] | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=100)] = 20,
) -> list[TimelineEntryResponse]:
    """Phase 2E: Plane's history of one work item, each change labelled with where it came
    from. `source`/`kind` filter it (searchable history)."""
    entries = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        project_id=project_id,
        work_item_id=work_item_id,
        source=source,
        kind=kind,
        limit=limit,
        correlation_id=_correlation_id(),
    )
    return [
        TimelineEntryResponse(
            at=e.at,
            source=e.source,
            kind=e.kind,
            actor_id=e.actor_id,
            actor_name=e.actor_name,
            old_value=e.old_value,
            new_value=e.new_value,
        )
        for e in entries
    ]


@router.get("/{work_item_id}", response_model=WorkItemResponse)
async def get_work_item(
    work_item_id: str,
    use_case: Annotated[GetWorkItem, Depends(get_get_work_item)],
    prom_instance_id: Annotated[UUID, Query()],
    prom_user_id: Annotated[str, Query()],
    workspace_slug: Annotated[str, Query()],
    project_id: Annotated[str, Query()],
) -> WorkItemResponse:
    work_item = await use_case.execute(
        prom_instance_id=prom_instance_id,
        prom_user_id=prom_user_id,
        workspace_slug=workspace_slug,
        project_id=project_id,
        work_item_id=work_item_id,
        correlation_id=_correlation_id(),
    )
    return _work_item_response(work_item)
