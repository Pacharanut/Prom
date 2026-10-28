"""Ports for the AI layer. Framework-free: the HTTP client for the LLM gateway lives in
src/adapters/ai/, the Postgres repository in src/adapters/persistence/, the Redis focus store in
src/adapters/redis/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from src.domain.ai.entities import AiSettings


@dataclass(frozen=True)
class AiConnection:
    """Everything needed to make one call to an OpenAI-compatible gateway. Holds the plaintext
    API key — never log or serialize this object."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int

    def __repr__(self) -> str:
        return f"AiConnection(base_url={self.base_url!r}, model={self.model!r}, api_key=***)"


@dataclass(frozen=True)
class AiDefaults:
    """The Hub's environment defaults (NT_AI_HUB_*), used for a customer that has no
    AiSettings row yet and for any field the row leaves unset (the API key). Built by the
    composition root from Settings, so the application layer never reads the environment."""

    is_enabled: bool
    base_url: str | None
    api_key: str | None
    model: str
    timeout_seconds: int


@dataclass
class LlmModelInfo:
    id: str
    modality: str | None
    """e.g. "chat", "embedding", "stt" when the gateway reports it (NT AI Hub does); None when
    a plain OpenAI-compatible server doesn't."""
    capabilities: list[str] | None = None
    """NT AI Hub also reports capabilities, which is the reliable signal: on 2026-10-26 it listed
    Qwen3.8-27B as modality "vision-ocr" but capabilities ["chat"], and an ASR model as modality
    "chat" but capabilities ["stt"]."""

    @property
    def is_chat(self) -> bool:
        if self.capabilities is not None:
            return "chat" in self.capabilities
        return self.modality in (None, "chat")


class LlmClient(Protocol):
    async def complete_json(
        self,
        connection: AiConnection,
        *,
        system_prompt: str,
        user_message: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """One chat-completions call that must come back as a JSON object. Raises
        AiUnavailableError (unreachable/timeout/5xx/non-API URL), AiCredentialInvalidError
        (401/403), or AiResponseInvalidError (reply was not a JSON object)."""
        ...

    async def list_models(self, connection: AiConnection) -> list[LlmModelInfo]: ...


class AiSettingsRepository(Protocol):
    async def get_by_customer(self, customer_id: UUID) -> AiSettings | None: ...
    async def save(self, settings: AiSettings) -> None:
        """Insert or update — one row per customer."""
        ...


@dataclass
class WorkItemFocus:
    """The work item a user was last talking about, so "task นี้" / "mark complete" can refer to
    it without naming it again."""

    project_id: str
    work_item_id: str
    label: str


class FocusStore(Protocol):
    async def get(
        self, channel_installation_id: UUID, channel_user_id: str
    ) -> WorkItemFocus | None: ...

    async def set(
        self,
        channel_installation_id: UUID,
        channel_user_id: str,
        focus: WorkItemFocus,
        ttl_seconds: int,
    ) -> None: ...
