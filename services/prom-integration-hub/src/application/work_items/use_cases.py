"""Work-item use cases — application layer. Shared by every channel (LINE, Discord, ...); only
the presenter/adapter differs per channel, per the master prompt's "one Application Use Case
shared across channels" rule. Framework-free, same as account_linking/use_cases.py.

Hub never persists work items (Prom/Plane owns them) — every use case here resolves a delegated
credential, then calls PromApiClient live. See `_resolve_delegated_context`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, NoReturn
from uuid import UUID

from src.application.account_linking.ports import (
    AuditLogRepository,
    Clock,
    DelegatedCredentialRepository,
    PromInstanceRepository,
    SecretProvider,
)
from src.application.prom_api.ports import PromApiClient, WorkItemPage
from src.application.shared.audit_writer import AuditWriter
from src.domain.audit.entities import AuditOrigin, ResultStatus
from src.domain.work_items.entities import (
    ProjectMemberRef,
    ProjectRef,
    WorkItem,
    WorkItemLabel,
    WorkItemSearchResult,
    WorkItemState,
)
from src.shared.errors import (
    DomainError,
    IdentityNotLinkedError,
    NotFoundError,
    PromRequestInvalidError,
)

_MAX_PROJECTS_PER_CALL = 10
_MAX_PAGES_PER_PROJECT = 3
_LIST_PER_PAGE = 100
"""ListMyWorkItems caps — Plane's public API has no server-side assignee/due-date filter (Gate
0/4 finding: pql/filters params are rejected with HTTP 400), so "my tasks" style views must list
every work item per project and filter client-side. Unbounded, this is a real production
performance risk (a workspace with many large projects would mean many sequential paginated
fetches per request) — these caps are a deliberate, documented Phase 1 tradeoff, not an oversight.
See README "Known limitations"."""


@dataclass
class DelegatedContext:
    customer_id: UUID
    base_url: str
    token: str


async def resolve_delegated_context(
    *,
    prom_instance_id: UUID,
    prom_user_id: str,
    prom_instances: PromInstanceRepository,
    delegated_credentials: DelegatedCredentialRepository,
    secrets_provider: SecretProvider,
) -> DelegatedContext:
    """Shared by every use case below — factored out because it was identical in all of them.
    Raises NotFoundError if the prom_instance is unknown, IdentityNotLinkedError if no delegated
    credential exists for (prom_instance_id, prom_user_id) yet (the user hasn't completed account
    linking, or unlinked)."""
    instance = await prom_instances.get(prom_instance_id)
    if instance is None:
        raise NotFoundError(f"prom_instance {prom_instance_id} not found")

    credential = await delegated_credentials.get(prom_instance_id, prom_user_id)
    if credential is None:
        raise IdentityNotLinkedError(
            f"no delegated credential for prom_user_id={prom_user_id} on "
            f"prom_instance_id={prom_instance_id} — account linking required first"
        )

    token = secrets_provider.decrypt(credential.encrypted_token).decode("utf-8")
    return DelegatedContext(
        customer_id=credential.customer_id, base_url=instance.base_url, token=token
    )


@dataclass
class MyTasksResult:
    items: list[WorkItem]
    truncated: bool
    """True when more than _MAX_PROJECTS_PER_CALL project_ids were passed in — the extras were
    silently dropped, not searched. Callers should surface this to the user (e.g. "showing results
    from your first 10 projects")."""


@dataclass
class _WorkItemUseCaseBase:
    prom_instances: PromInstanceRepository
    delegated_credentials: DelegatedCredentialRepository
    secrets_provider: SecretProvider
    prom_api_client: PromApiClient
    audit_log: AuditLogRepository
    clock: Clock

    async def _resolve(self, *, prom_instance_id: UUID, prom_user_id: str) -> DelegatedContext:
        return await resolve_delegated_context(
            prom_instance_id=prom_instance_id,
            prom_user_id=prom_user_id,
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
        )

    def _audit(self) -> AuditWriter:
        return AuditWriter(self.audit_log, self.clock)


@dataclass
class CreateWorkItem(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        title: str,
        state_id: str | None = None,
        assignee_ids: list[str] | None = None,
        due_date: date | None = None,
        description_html: str | None = None,
        idempotency_key: str | None = None,
        priority: str | None = None,
        label_ids: list[str] | None = None,
        origin: AuditOrigin | None = None,
        correlation_id: str,
    ) -> tuple[WorkItem, bool]:
        """Returns (work_item, was_replay). Two independent dedup layers are deliberate, not
        redundant: (1) the Hub-side Idempotency-Key cache (apps/api layer, see
        apps/api/idempotency.py) avoids re-invoking this use case — and re-calling Prom — at all
        on a literal client retry; (2) Prom's own external_id/external_source dedup (passed on
        every call below) still protects even if the Hub-side cache was bypassed, expired, or the
        caller never sent an Idempotency-Key header at all. Do not remove either thinking the
        other makes it redundant — they cover different failure modes.
        """
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=origin.installation_id if origin else None,
                channel_type=origin.channel_type if origin else None,
                channel_user_id=origin.channel_user_id if origin else None,
                prom_user_id=prom_user_id,
                action="work_item.create_failed",
                target_type="work_item",
                target_id=None,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        try:
            work_item, was_replay = await self.prom_api_client.create_work_item(
                ctx.base_url,
                ctx.token,
                workspace_slug,
                project_id,
                name=title,
                state_id=state_id,
                assignee_ids=assignee_ids,
                target_date=due_date,
                description_html=description_html,
                external_id=idempotency_key or correlation_id,
                external_source="prom-integration-hub",
                priority=priority,
                label_ids=label_ids,
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=origin.installation_id if origin else None,
            channel_type=origin.channel_type if origin else None,
            channel_user_id=origin.channel_user_id if origin else None,
            prom_user_id=prom_user_id,
            action="work_item.create_replayed" if was_replay else "work_item.created",
            target_type="work_item",
            target_id=work_item.id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return work_item, was_replay


@dataclass
class GetWorkItem(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        correlation_id: str,
    ) -> WorkItem:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        return await self.prom_api_client.get_work_item(
            ctx.base_url, ctx.token, workspace_slug, project_id, work_item_id
        )


@dataclass
class SearchWorkItems(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        query: str,
        limit: int = 10,
        workspace_search: bool = True,
        project_id: str | None = None,
        correlation_id: str,
    ) -> list[WorkItemSearchResult]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        return await self.prom_api_client.search_work_items(
            ctx.base_url,
            ctx.token,
            workspace_slug,
            query=query,
            limit=limit,
            workspace_search=workspace_search,
            project_id=project_id,
        )


@dataclass
class ListProjects(_WorkItemUseCaseBase):
    """Not in the original Gate 4 spec's use-case list, but needed by the my-tasks/create flows
    (a channel needs to show the user a project picker) and trivial to add given the port already
    exists — kept minimal, no caching."""

    async def execute(
        self, *, prom_instance_id: UUID, prom_user_id: str, workspace_slug: str, correlation_id: str
    ) -> list[ProjectRef]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        return await self.prom_api_client.list_projects(ctx.base_url, ctx.token, workspace_slug)


@dataclass
class ListStates(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        correlation_id: str,
    ) -> list[WorkItemState]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        return await self.prom_api_client.list_states(
            ctx.base_url, ctx.token, workspace_slug, project_id
        )


@dataclass
class ListProjectMembers(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        correlation_id: str,
    ) -> list[ProjectMemberRef]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        return await self.prom_api_client.list_project_members(
            ctx.base_url, ctx.token, workspace_slug, project_id
        )


@dataclass
class ListMyWorkItems(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_ids: list[str],
        view: Literal["assigned", "due_today", "overdue", "due_soon"],
        correlation_id: str,
    ) -> MyTasksResult:
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=None,
                channel_type=None,
                channel_user_id=None,
                prom_user_id=prom_user_id,
                action="work_item.my_tasks_query_failed",
                target_type="work_item",
                target_id=None,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        truncated = len(project_ids) > _MAX_PROJECTS_PER_CALL
        capped_project_ids = project_ids[:_MAX_PROJECTS_PER_CALL]
        today = self.clock.now().date()
        matched: list[WorkItem] = []

        for pid in capped_project_ids:
            cursor: str | None = None
            for _page_number in range(_MAX_PAGES_PER_PROJECT):
                try:
                    page: WorkItemPage = await self.prom_api_client.list_work_items(
                        ctx.base_url,
                        ctx.token,
                        workspace_slug,
                        pid,
                        cursor=cursor,
                        per_page=_LIST_PER_PAGE,
                    )
                except DomainError as exc:
                    await _fail(exc, ctx.customer_id)

                for item in page.items:
                    if prom_user_id not in item.assignee_ids:
                        continue
                    if view == "assigned":
                        matched.append(item)
                    elif view == "due_today":
                        if item.target_date == today:
                            matched.append(item)
                    elif view == "overdue":
                        if (
                            item.target_date is not None
                            and item.target_date < today
                            and item.completed_at is None
                        ):
                            matched.append(item)
                    elif view == "due_soon":
                        # "Due tomorrow" — Gate 7's CheckDueDateNotifications' one-day-ahead
                        # reminder (§8.6 "Due date ก่อนหนึ่งวัน").
                        if item.target_date == today + timedelta(days=1):
                            matched.append(item)

                if not page.has_more or page.next_cursor is None:
                    break
                cursor = page.next_cursor

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=None,
            channel_type=None,
            channel_user_id=None,
            prom_user_id=prom_user_id,
            action="work_item.my_tasks_queried",
            target_type="work_item",
            target_id=None,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return MyTasksResult(items=matched, truncated=truncated)


@dataclass
class UpdateWorkItemState(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        state_id: str,
        origin: AuditOrigin | None = None,
        correlation_id: str,
    ) -> WorkItem:
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=origin.installation_id if origin else None,
                channel_type=origin.channel_type if origin else None,
                channel_user_id=origin.channel_user_id if origin else None,
                prom_user_id=prom_user_id,
                action="work_item.update_failed",
                target_type="work_item",
                target_id=work_item_id,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        try:
            work_item = await self.prom_api_client.update_work_item(
                ctx.base_url,
                ctx.token,
                workspace_slug,
                project_id,
                work_item_id,
                fields={"state": state_id},
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=origin.installation_id if origin else None,
            channel_type=origin.channel_type if origin else None,
            channel_user_id=origin.channel_user_id if origin else None,
            prom_user_id=prom_user_id,
            action="work_item.updated",
            target_type="work_item",
            target_id=work_item.id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return work_item


@dataclass
class ChangeAssignee(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        assignee_ids: list[str],
        origin: AuditOrigin | None = None,
        correlation_id: str,
    ) -> WorkItem:
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=origin.installation_id if origin else None,
                channel_type=origin.channel_type if origin else None,
                channel_user_id=origin.channel_user_id if origin else None,
                prom_user_id=prom_user_id,
                action="work_item.assign_failed",
                target_type="work_item",
                target_id=work_item_id,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        try:
            work_item = await self.prom_api_client.update_work_item(
                ctx.base_url,
                ctx.token,
                workspace_slug,
                project_id,
                work_item_id,
                fields={"assignees": assignee_ids},
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=origin.installation_id if origin else None,
            channel_type=origin.channel_type if origin else None,
            channel_user_id=origin.channel_user_id if origin else None,
            prom_user_id=prom_user_id,
            action="work_item.assigned",
            target_type="work_item",
            target_id=work_item.id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return work_item


@dataclass
class AddComment(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        comment_html: str,
        origin: AuditOrigin | None = None,
        correlation_id: str,
    ) -> str:
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=origin.installation_id if origin else None,
                channel_type=origin.channel_type if origin else None,
                channel_user_id=origin.channel_user_id if origin else None,
                prom_user_id=prom_user_id,
                action="comment.create_failed",
                target_type="work_item",
                target_id=work_item_id,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        try:
            comment_id = await self.prom_api_client.add_comment(
                ctx.base_url,
                ctx.token,
                workspace_slug,
                project_id,
                work_item_id,
                comment_html=comment_html,
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=origin.installation_id if origin else None,
            channel_type=origin.channel_type if origin else None,
            channel_user_id=origin.channel_user_id if origin else None,
            prom_user_id=prom_user_id,
            action="comment.created",
            target_type="work_item",
            target_id=work_item_id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return comment_id


@dataclass
class MarkComplete(_WorkItemUseCaseBase):
    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        origin: AuditOrigin | None = None,
        correlation_id: str,
    ) -> WorkItem:
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=origin.installation_id if origin else None,
                channel_type=origin.channel_type if origin else None,
                channel_user_id=origin.channel_user_id if origin else None,
                prom_user_id=prom_user_id,
                action="work_item.complete_failed",
                target_type="work_item",
                target_id=work_item_id,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        try:
            states = await self.prom_api_client.list_states(
                ctx.base_url, ctx.token, workspace_slug, project_id
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        completed_state = next((s for s in states if s.group == "completed"), None)
        if completed_state is None:
            await _fail(
                PromRequestInvalidError(
                    f"project {project_id} has no state with group=='completed' to mark this "
                    "work item complete against"
                ),
                ctx.customer_id,
            )

        try:
            work_item = await self.prom_api_client.update_work_item(
                ctx.base_url,
                ctx.token,
                workspace_slug,
                project_id,
                work_item_id,
                fields={"state": completed_state.id},
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=origin.installation_id if origin else None,
            channel_type=origin.channel_type if origin else None,
            channel_user_id=origin.channel_user_id if origin else None,
            prom_user_id=prom_user_id,
            action="work_item.completed",
            target_type="work_item",
            target_id=work_item.id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return work_item


@dataclass
class RescheduleWorkItem(_WorkItemUseCaseBase):
    """Phase 2 (AI assistant, "เลื่อน deadline"): set a work item's due date. Plane's field is
    `target_date` (see WorkItem.target_date); `None` clears it."""

    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        due_date: date | None,
        origin: AuditOrigin | None = None,
        correlation_id: str,
    ) -> WorkItem:
        audit = self._audit()

        async def _fail(error: DomainError, customer_id: UUID | None = None) -> NoReturn:
            await audit.write(
                customer_id=customer_id,
                prom_instance_id=prom_instance_id,
                installation_id=origin.installation_id if origin else None,
                channel_type=origin.channel_type if origin else None,
                channel_user_id=origin.channel_user_id if origin else None,
                prom_user_id=prom_user_id,
                action="work_item.reschedule_failed",
                target_type="work_item",
                target_id=work_item_id,
                correlation_id=correlation_id,
                result_status=ResultStatus.FAILURE,
                error_code=error.error_code,
            )
            raise error

        try:
            ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        except DomainError as exc:
            await _fail(exc)

        try:
            work_item = await self.prom_api_client.update_work_item(
                ctx.base_url,
                ctx.token,
                workspace_slug,
                project_id,
                work_item_id,
                fields={"target_date": due_date.isoformat() if due_date else None},
            )
        except DomainError as exc:
            await _fail(exc, ctx.customer_id)

        await audit.write(
            customer_id=ctx.customer_id,
            prom_instance_id=prom_instance_id,
            installation_id=origin.installation_id if origin else None,
            channel_type=origin.channel_type if origin else None,
            channel_user_id=origin.channel_user_id if origin else None,
            prom_user_id=prom_user_id,
            action="work_item.rescheduled",
            target_type="work_item",
            target_id=work_item.id,
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return work_item


@dataclass
class ListProjectWorkItems(_WorkItemUseCaseBase):
    """Phase 2 (AI search by project / by person): the first page of one project's work items,
    newest first, optionally filtered to one assignee. One page only (same documented-cap
    reasoning as ListMyWorkItems) — this backs a chat reply, not a report."""

    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        assignee_id: str | None = None,
        limit: int = 10,
        correlation_id: str,
    ) -> list[WorkItem]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        page = await self.prom_api_client.list_work_items(
            ctx.base_url, ctx.token, workspace_slug, project_id, per_page=_LIST_PER_PAGE
        )
        items = [
            item for item in page.items if assignee_id is None or assignee_id in item.assignee_ids
        ]
        items.sort(key=lambda item: item.sequence_id, reverse=True)
        return items[:limit]


_MAX_BLOCKED_CHECKS = 20
"""FindBlockedWorkItems checks at most this many work items (one relations call each, plus one
call per blocker) — it backs one line of a daily summary, not a dependency report."""


@dataclass
class FindBlockedWorkItems(_WorkItemUseCaseBase):
    """Phase 2B (daily summary): which of the given work items are blocked by a work item that
    isn't finished yet. Plane relations only give the blocker's id, so each blocker is fetched to
    check `completed_at` (a blocker the user can't read counts as unfinished — it still blocks).
    Best-effort: a relations lookup that fails is skipped, never raised."""

    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        work_items: list[WorkItem],
        correlation_id: str,
    ) -> list[WorkItem]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        blocked: list[WorkItem] = []
        finished_cache: dict[str, bool] = {}
        for item in work_items[:_MAX_BLOCKED_CHECKS]:
            try:
                blockers = await self.prom_api_client.list_blocked_by(
                    ctx.base_url, ctx.token, workspace_slug, item.project_id, item.id
                )
            except DomainError:
                continue
            for blocker_project_id, blocker_id in blockers:
                if blocker_id not in finished_cache:
                    try:
                        blocker = await self.prom_api_client.get_work_item(
                            ctx.base_url, ctx.token, workspace_slug, blocker_project_id, blocker_id
                        )
                        finished_cache[blocker_id] = blocker.completed_at is not None
                    except DomainError:
                        finished_cache[blocker_id] = False
                if not finished_cache[blocker_id]:
                    blocked.append(item)
                    break
        return blocked


@dataclass
class ListLabels(_WorkItemUseCaseBase):
    """Phase 2C: a project's labels, for auto-tagging (names → ids for this project)."""

    async def execute(
        self,
        *,
        prom_instance_id: UUID,
        prom_user_id: str,
        workspace_slug: str,
        project_id: str,
        correlation_id: str,
    ) -> list[WorkItemLabel]:
        ctx = await self._resolve(prom_instance_id=prom_instance_id, prom_user_id=prom_user_id)
        return await self.prom_api_client.list_labels(
            ctx.base_url, ctx.token, workspace_slug, project_id
        )
