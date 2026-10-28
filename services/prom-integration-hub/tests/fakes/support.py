"""Fake Clock, SecretProvider, RateLimiter, ChannelCredentialValidator, PromApiClient,
WebhookDedupStore, and ConversationStateRepository.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from src.application.account_linking.ports import (
    ChannelCredentialValidator,
    Clock,
    RateLimiter,
    SecretProvider,
    ValidationResult,
)
from src.application.ai.ports import AiConnection, LlmClient, LlmModelInfo, WorkItemFocus
from src.application.notifications.ports import MessagePublisher
from src.application.prom_api.ports import PromApiClient, PromUserProfile, WorkItemPage
from src.application.webhooks.ports import WebhookDedupStore
from src.application.work_items.conversation import ConversationState
from src.application.work_items.ports import ConversationStateRepository
from src.domain.integrations.entities import ChannelType
from src.domain.work_items.entities import (
    ProjectMemberRef,
    ProjectRef,
    WorkItem,
    WorkItemActivity,
    WorkItemLabel,
    WorkItemSearchResult,
    WorkItemState,
)
from src.shared.errors import (
    ChannelDeliveryFailedError,
    DomainError,
    PromApiUnavailableError,
    WorkItemNotFoundError,
)


class FakeClock(Clock):
    def __init__(self, initial: datetime) -> None:
        self._current = initial

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> None:
        self._current = value

    def advance(self, delta: timedelta) -> None:
        self._current += delta


class FakeSecretProvider(SecretProvider):
    """Reversible but NOT real cryptography — deterministic base64 round-trip, fast for tests.
    Never use outside tests; see FernetSecretProvider for the real implementation."""

    def encrypt(self, plaintext: bytes) -> bytes:
        return base64.b64encode(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return base64.b64decode(ciphertext)


class FakeRateLimiter(RateLimiter):
    """Ignores window_seconds — tests only exercise "under limit" vs "over limit" within a
    single logical window, which is all StartAccountLinking/ConsumeLinkingCode need."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def check_and_increment(self, key: str, limit: int, window_seconds: int) -> bool:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key] <= limit


@dataclass
class FakeChannelCredentialValidator(ChannelCredentialValidator):
    is_valid: bool = True
    external_account_id: str | None = "external-account-1"
    error_message: str | None = None

    async def validate(
        self, channel_type: ChannelType, credentials: dict[str, str]
    ) -> ValidationResult:
        return ValidationResult(
            is_valid=self.is_valid,
            external_account_id=self.external_account_id if self.is_valid else None,
            error_message=self.error_message,
        )


@dataclass
class FakePromApiClient(PromApiClient):
    """In-memory Prom API double covering both Gate 3 (get_current_user) and Gate 4 (work-item
    CRUD/list/search) surface. Configure the public fields directly in a test before exercising a
    use case — see tests/application/test_work_items.py for examples of every knob:

    - `projects`/`states_by_project`/`members_by_project`: seed lookup data.
    - `pages_by_project`: pre-split pages for list_work_items, keyed by project_id, each a list of
      pages (list[list[WorkItem]]) — lets ListMyWorkItems's pagination-cap logic be exercised
      deterministically without a real cursor protocol.
    - `create_error`/`list_error`/`update_error`/`comment_error`: raise on the next call to that
      method, to exercise DomainError propagation/audit-on-failure paths.
    - external_id/external_source dedup on create_work_item is simulated for real (not just
      stubbed), so the 409-then-refetch "idempotent replay" behavior can be tested honestly.
    """

    profile_id_override: str | None = None
    error_to_raise: DomainError | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    projects: list[ProjectRef] = field(default_factory=list)
    states_by_project: dict[str, list[WorkItemState]] = field(default_factory=dict)
    members_by_project: dict[str, list[ProjectMemberRef]] = field(default_factory=dict)
    pages_by_project: dict[str, list[list[WorkItem]]] = field(default_factory=dict)
    search_results: list[WorkItemSearchResult] = field(default_factory=list)
    blocked_by: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    labels_by_project: dict[str, list[WorkItemLabel]] = field(default_factory=dict)
    activities: dict[str, list[WorkItemActivity]] = field(default_factory=dict)
    created_extras: dict[str, dict[str, object]] = field(default_factory=dict)
    """work_item_id -> {"priority", "label_ids"} as passed to create_work_item (Phase 2C)."""
    """work_item_id -> [(project_id, blocker_work_item_id)] for list_blocked_by."""

    create_error: DomainError | None = None
    list_error: DomainError | None = None
    update_error: DomainError | None = None
    comment_error: DomainError | None = None

    list_calls: list[tuple[str, str | None]] = field(default_factory=list)
    """(project_id, cursor) for every list_work_items call — asserted against in the my-tasks
    cap tests to prove pagination actually stopped where expected."""

    work_items: dict[str, WorkItem] = field(default_factory=dict, repr=False)
    _external_index: dict[tuple[str, str, str], str] = field(default_factory=dict, repr=False)
    _next_seq: int = field(default=1, repr=False)

    async def get_current_user(self, base_url: str, token: str) -> PromUserProfile:
        self.calls.append((base_url, token))
        if self.error_to_raise is not None:
            raise self.error_to_raise
        return PromUserProfile(
            id=self.profile_id_override or "prom-user-1",
            email="user@example.com",
            display_name="Test User",
        )

    async def list_projects(
        self, base_url: str, token: str, workspace_slug: str
    ) -> list[ProjectRef]:
        return list(self.projects)

    async def list_states(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[WorkItemState]:
        return list(self.states_by_project.get(project_id, []))

    async def list_project_members(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[ProjectMemberRef]:
        return list(self.members_by_project.get(project_id, []))

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
        target_date: object,
        description_html: str | None,
        external_id: str,
        external_source: str,
        priority: str | None = None,
        label_ids: list[str] | None = None,
    ) -> tuple[WorkItem, bool]:
        if self.create_error is not None:
            raise self.create_error
        key = (project_id, external_source, external_id)
        existing_id = self._external_index.get(key)
        if existing_id is not None:
            return self.work_items[existing_id], True

        item_id = f"wi-{self._next_seq}"
        item = WorkItem(
            id=item_id,
            sequence_id=self._next_seq,
            project_id=project_id,
            name=name,
            state_id=state_id,
            priority=None,
            assignee_ids=list(assignee_ids or []),
            target_date=target_date,  # type: ignore[arg-type]
            completed_at=None,
        )
        self._next_seq += 1
        self.work_items[item_id] = item
        self.created_extras[item_id] = {"priority": priority, "label_ids": list(label_ids or [])}
        self._external_index[key] = item_id
        return item, False

    async def get_work_item(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> WorkItem:
        item = self.work_items.get(work_item_id)
        if item is None:
            raise WorkItemNotFoundError(f"no such work item {work_item_id}")
        return item

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
        if self.list_error is not None:
            raise self.list_error
        self.list_calls.append((project_id, cursor))

        pages = self.pages_by_project.get(project_id)
        if pages is None:
            items = [wi for wi in self.work_items.values() if wi.project_id == project_id]
            return WorkItemPage(items=items, next_cursor=None, has_more=False)

        page_index = int(cursor) if cursor is not None else 0
        page_items = pages[page_index] if page_index < len(pages) else []
        has_more = page_index + 1 < len(pages)
        next_cursor = str(page_index + 1) if has_more else None
        return WorkItemPage(items=page_items, next_cursor=next_cursor, has_more=has_more)

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
        if self.update_error is not None:
            raise self.update_error
        item = self.work_items.get(work_item_id)
        if item is None:
            raise WorkItemNotFoundError(f"no such work item {work_item_id}")
        if "state" in fields:
            item.state_id = fields["state"]  # type: ignore[assignment]
            # Mirrors real Prom/Plane: the server sets completed_at itself when a work item's
            # state moves into a group=="completed" state, and clears it otherwise. MarkComplete
            # (src/application/work_items/use_cases.py) trusts update_work_item's returned
            # `completed_at` rather than computing it, exactly like the real HTTP client would.
            new_state = next(
                (s for s in self.states_by_project.get(project_id, []) if s.id == item.state_id),
                None,
            )
            item.completed_at = (
                datetime.now(UTC)
                if new_state is not None and new_state.group == "completed"
                else None
            )
        if "assignees" in fields:
            item.assignee_ids = list(fields["assignees"])  # type: ignore[arg-type]
        if "target_date" in fields:
            raw = fields["target_date"]
            item.target_date = date.fromisoformat(raw) if isinstance(raw, str) else None
        return item

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
        if self.comment_error is not None:
            raise self.comment_error
        return f"comment-{work_item_id}-1"

    async def list_work_item_activities(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> list[WorkItemActivity]:
        return sorted(
            self.activities.get(work_item_id, []), key=lambda a: a.created_at, reverse=True
        )

    async def list_labels(
        self, base_url: str, token: str, workspace_slug: str, project_id: str
    ) -> list[WorkItemLabel]:
        return list(self.labels_by_project.get(project_id, []))

    async def list_blocked_by(
        self, base_url: str, token: str, workspace_slug: str, project_id: str, work_item_id: str
    ) -> list[tuple[str, str]]:
        return list(self.blocked_by.get(work_item_id, []))

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
        return list(self.search_results)


class AlwaysUnavailablePromApiClient(PromApiClient):
    async def get_current_user(self, base_url: str, token: str) -> PromUserProfile:
        raise PromApiUnavailableError("simulated outage")


class FakeWebhookDedupStore(WebhookDedupStore):
    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def mark_seen_if_new(self, event_key: str, ttl_seconds: int) -> bool:
        if event_key in self._seen:
            return False
        self._seen.add(event_key)
        return True


class FakeConversationStateRepository(ConversationStateRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str], ConversationState] = {}

    async def get(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> ConversationState | None:
        return self._items.get((channel_installation_id, channel_user_id))

    async def set(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        state: ConversationState,
        ttl_seconds: int,
    ) -> None:
        self._items[(channel_installation_id, channel_user_id)] = state

    async def clear(self, channel_installation_id: UUID, channel_user_id: str) -> None:
        self._items.pop((channel_installation_id, channel_user_id), None)


@dataclass
class FakeMessagePublisher(MessagePublisher):
    """Gate 7: records every send for assertions; `fail_for` optionally makes the next N sends
    raise ChannelDeliveryFailedError, to exercise SendPrivateNotification's/FlushOutboundMessage's
    retry-on-failure paths without needing a real LINE/Discord API.
    """

    sent: list[tuple[ChannelType, str, str]] = field(default_factory=list)
    """(channel_type, channel_user_id, text) for every successful send."""
    fail_next: int = 0

    async def send_private_message(
        self,
        channel_type: ChannelType,
        credentials: dict[str, str],
        channel_user_id: str,
        text: str,
    ) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ChannelDeliveryFailedError(f"simulated delivery failure to {channel_user_id}")
        self.sent.append((channel_type, channel_user_id, text))


@dataclass
class FakeLlmClient(LlmClient):
    """Returns a canned command per exact user message (`replies`), else `default`. Records
    every call so tests can assert the prompt carried today's date and project names."""

    replies: dict[str, dict[str, Any]] = field(default_factory=dict)
    default: dict[str, Any] = field(default_factory=lambda: {"intent": "unknown"})
    error: DomainError | None = None
    models: list[LlmModelInfo] = field(
        default_factory=lambda: [
            LlmModelInfo(id="gpt-oss-120b", modality="chat"),
            LlmModelInfo(id="Qwen3-Embedding-4B", modality="embedding"),
        ]
    )
    models_error: DomainError | None = None
    calls: list[tuple[AiConnection, str, str]] = field(default_factory=list)
    """(connection, system_prompt, user_message)."""

    async def complete_json(
        self,
        connection: AiConnection,
        *,
        system_prompt: str,
        user_message: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((connection, system_prompt, user_message))
        if self.error is not None:
            raise self.error
        return dict(self.replies.get(user_message, self.default))

    async def list_models(self, connection: AiConnection) -> list[LlmModelInfo]:
        if self.models_error is not None:
            raise self.models_error
        return list(self.models)


class FakeFocusStore:
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, str], WorkItemFocus] = {}

    async def get(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> WorkItemFocus | None:
        return self._items.get((channel_installation_id, channel_user_id))

    async def set(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        focus: WorkItemFocus,
        ttl_seconds: int,
    ) -> None:
        self._items[(channel_installation_id, channel_user_id)] = focus
