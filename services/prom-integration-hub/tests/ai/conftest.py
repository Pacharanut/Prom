"""Harness for AiAssistant / AI settings tests: real use cases over the shared fakes, a
FakeLlmClient standing in for the gateway, and one linked user in one workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.application.ai.assistant import AiAssistant, AssistantSession
from src.application.ai.intent_parser import IntentParser
from src.application.ai.ports import AiDefaults
from src.application.ai.settings import (
    GetAiSettings,
    ResolveAiConnection,
    TestAiConnection,
    UpdateAiSettings,
)
from src.application.work_items.conversation import FlowContext
from src.application.work_items.use_cases import (
    AddComment,
    ChangeAssignee,
    CreateWorkItem,
    GetWorkItem,
    ListMyWorkItems,
    ListProjectMembers,
    ListProjects,
    ListProjectWorkItems,
    ListStates,
    MarkComplete,
    RescheduleWorkItem,
    SearchWorkItems,
    UpdateWorkItemState,
)
from src.domain.ai.entities import AiSettings
from src.domain.customers.entities import Customer, CustomerStatus
from src.domain.identities.entities import DelegatedCredential
from src.domain.instances.entities import InstanceStatus, PromInstance
from src.domain.integrations.entities import ChannelType
from src.domain.work_items.entities import ProjectMemberRef, ProjectRef, WorkItem, WorkItemState
from tests.fakes.repositories import (
    FakeAiSettingsRepository,
    FakeAuditLogRepository,
    FakeCustomerRepository,
    FakeDelegatedCredentialRepository,
    FakePromInstanceRepository,
)
from tests.fakes.support import (
    FakeClock,
    FakeConversationStateRepository,
    FakeFocusStore,
    FakeLlmClient,
    FakePromApiClient,
    FakeRateLimiter,
    FakeSecretProvider,
)

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)  # Monday 10:00 Asia/Bangkok
PROM_USER = "u-me"
CHANNEL_USER = "U-line-1"

AI_PROJECT = ProjectRef(id="p-ai", name="AI Platform", identifier="AI")
INFRA_PROJECT = ProjectRef(id="p-infra", name="Infrastructure", identifier="INFRA")
STATES = [
    WorkItemState(id="s-todo", name="Todo", group="unstarted"),
    WorkItemState(id="s-doing", name="In Progress", group="started"),
    WorkItemState(id="s-done", name="Done", group="completed"),
]
MEMBERS = [
    ProjectMemberRef(user_id=PROM_USER, display_name="Me"),
    ProjectMemberRef(user_id="u-somchai", display_name="Somchai"),
    ProjectMemberRef(user_id="u-nok", display_name="Nok"),
]


@dataclass
class AiHarness:
    customers: FakeCustomerRepository
    prom_instances: FakePromInstanceRepository
    delegated_credentials: FakeDelegatedCredentialRepository
    audit_log: FakeAuditLogRepository
    clock: FakeClock
    secrets_provider: FakeSecretProvider
    prom_api_client: FakePromApiClient
    llm_client: FakeLlmClient
    ai_settings: FakeAiSettingsRepository
    conversation_states: FakeConversationStateRepository
    focus_store: FakeFocusStore
    rate_limiter: FakeRateLimiter
    defaults: AiDefaults
    customer_id: UUID
    prom_instance_id: UUID
    installation_id: UUID

    def _uc(self, cls: Any) -> Any:
        return cls(
            prom_instances=self.prom_instances,
            delegated_credentials=self.delegated_credentials,
            secrets_provider=self.secrets_provider,
            prom_api_client=self.prom_api_client,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def resolve_connection(self) -> ResolveAiConnection:
        return ResolveAiConnection(
            ai_settings=self.ai_settings,
            secrets_provider=self.secrets_provider,
            defaults=self.defaults,
        )

    @property
    def assistant(self) -> AiAssistant:
        return AiAssistant(
            resolve_connection=self.resolve_connection,
            intent_parser=IntentParser(llm_client=self.llm_client),
            conversation_states=self.conversation_states,
            focus_store=self.focus_store,
            rate_limiter=self.rate_limiter,
            prom_instances=self.prom_instances,
            audit_log=self.audit_log,
            clock=self.clock,
            list_projects=self._uc(ListProjects),
            list_states=self._uc(ListStates),
            list_project_members=self._uc(ListProjectMembers),
            list_my_work_items=self._uc(ListMyWorkItems),
            list_project_work_items=self._uc(ListProjectWorkItems),
            search_work_items=self._uc(SearchWorkItems),
            get_work_item=self._uc(GetWorkItem),
            create_work_item=self._uc(CreateWorkItem),
            update_work_item_state=self._uc(UpdateWorkItemState),
            mark_complete=self._uc(MarkComplete),
            change_assignee=self._uc(ChangeAssignee),
            reschedule_work_item=self._uc(RescheduleWorkItem),
            add_comment=self._uc(AddComment),
        )

    @property
    def get_settings(self) -> GetAiSettings:
        return GetAiSettings(
            customers=self.customers, ai_settings=self.ai_settings, defaults=self.defaults
        )

    @property
    def update_settings(self) -> UpdateAiSettings:
        return UpdateAiSettings(
            customers=self.customers,
            ai_settings=self.ai_settings,
            secrets_provider=self.secrets_provider,
            defaults=self.defaults,
            audit_log=self.audit_log,
            clock=self.clock,
        )

    @property
    def test_connection(self) -> TestAiConnection:
        return TestAiConnection(
            customers=self.customers,
            ai_settings=self.ai_settings,
            secrets_provider=self.secrets_provider,
            defaults=self.defaults,
            llm_client=self.llm_client,
            intent_parser=IntentParser(llm_client=self.llm_client),
            clock=self.clock,
        )

    def session(self) -> AssistantSession:
        return AssistantSession(
            customer_id=self.customer_id,
            channel_installation_id=self.installation_id,
            channel_type=ChannelType.LINE,
            channel_user_id=CHANNEL_USER,
            workspace_connection_id=uuid4(),
            flow=FlowContext(
                prom_instance_id=self.prom_instance_id,
                prom_user_id=PROM_USER,
                workspace_slug="acme",
                correlation_id="corr-1",
            ),
        )

    async def enable_ai(self, *, api_key: str = "custom-key", model: str = "gpt-oss-120b") -> None:
        await self.ai_settings.save(
            AiSettings(
                id=uuid4(),
                customer_id=self.customer_id,
                is_enabled=True,
                base_url="https://aihub.example/core-api/v1",
                encrypted_api_key=self.secrets_provider.encrypt(api_key.encode()),
                model=model,
                timeout_seconds=15,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    def seed_work_item(
        self,
        item_id: str,
        name: str,
        *,
        project: ProjectRef = AI_PROJECT,
        seq: int = 1,
        assignees: list[str] | None = None,
        due: date | None = None,
        state_id: str | None = "s-todo",
    ) -> WorkItem:
        item = WorkItem(
            id=item_id,
            sequence_id=seq,
            project_id=project.id,
            name=name,
            state_id=state_id,
            priority=None,
            assignee_ids=list(assignees or []),
            target_date=due,
            completed_at=None,
        )
        self.prom_api_client.work_items[item_id] = item
        return item

    def says(self, text: str, **command: Any) -> None:
        """The fake LLM returns `command` when it receives exactly `text`."""
        self.llm_client.replies[text] = command


@pytest.fixture
async def ai_harness() -> AiHarness:
    customers = FakeCustomerRepository()
    prom_instances = FakePromInstanceRepository()
    delegated_credentials = FakeDelegatedCredentialRepository()
    secrets_provider = FakeSecretProvider()
    customer = Customer(
        id=uuid4(), name="Acme", status=CustomerStatus.ACTIVE, created_at=NOW, updated_at=NOW
    )
    await customers.add(customer)
    instance = PromInstance(
        id=uuid4(),
        customer_id=customer.id,
        name="Acme Prom",
        base_url="https://prom.acme.example",
        status=InstanceStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    await prom_instances.add(instance)
    await delegated_credentials.upsert(
        DelegatedCredential(
            id=uuid4(),
            customer_id=customer.id,
            prom_instance_id=instance.id,
            prom_user_id=PROM_USER,
            encrypted_token=secrets_provider.encrypt(b"delegated-token"),
            created_at=NOW,
            updated_at=NOW,
            rotated_at=None,
        )
    )
    prom_api_client = FakePromApiClient(
        projects=[AI_PROJECT, INFRA_PROJECT],
        states_by_project={AI_PROJECT.id: list(STATES), INFRA_PROJECT.id: list(STATES)},
        members_by_project={AI_PROJECT.id: list(MEMBERS), INFRA_PROJECT.id: list(MEMBERS)},
    )
    return AiHarness(
        customers=customers,
        prom_instances=prom_instances,
        delegated_credentials=delegated_credentials,
        audit_log=FakeAuditLogRepository(),
        clock=FakeClock(NOW),
        secrets_provider=secrets_provider,
        prom_api_client=prom_api_client,
        llm_client=FakeLlmClient(),
        ai_settings=FakeAiSettingsRepository(),
        conversation_states=FakeConversationStateRepository(),
        focus_store=FakeFocusStore(),
        rate_limiter=FakeRateLimiter(),
        defaults=AiDefaults(
            is_enabled=False,
            base_url="https://aihub.example/core-api/v1",
            api_key="env-key",
            model="gpt-oss-120b",
            timeout_seconds=15,
        ),
        customer_id=customer.id,
        prom_instance_id=instance.id,
        installation_id=uuid4(),
    )
