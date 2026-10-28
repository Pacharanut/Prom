"""AI settings use cases: what Plane's Settings → AI Assistant page reads, saves, and tests, and
how the assistant decides which gateway/key/model to use for a customer.

Precedence: a customer's AiSettings row wins field-by-field; a row with no API key of its own
uses the Hub's NT_AI_HUB_KEY default; a customer with no row at all uses AiDefaults entirely.
The plaintext key is never returned — only `has_api_key` and where it comes from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from src.application.account_linking.ports import (
    AuditLogRepository,
    Clock,
    CustomerRepository,
    SecretProvider,
)
from src.application.ai.intent_parser import IntentParser
from src.application.ai.ports import AiConnection, AiDefaults, AiSettingsRepository, LlmClient
from src.application.shared.audit_writer import AuditWriter
from src.domain.ai.entities import AiSettings, InvalidAiSettingsError, validate_ai_connection_fields
from src.domain.audit.entities import ResultStatus
from src.shared.errors import (
    AiNotConfiguredError,
    AiSettingsInvalidError,
    DomainError,
    NotFoundError,
)

ApiKeySource = Literal["custom", "environment"]

_TEST_MESSAGE = "งานของฉันวันนี้"
"""A short, representative Thai command — the connection test checks the model can actually
produce our JSON schema, not just that the URL answers."""


@dataclass
class AiSettingsView:
    customer_id: UUID
    is_enabled: bool
    base_url: str | None
    model: str
    timeout_seconds: int
    has_api_key: bool
    api_key_source: ApiKeySource | None
    is_customized: bool
    """False while the customer is still on the Hub's environment defaults (no row saved)."""


def _view(customer_id: UUID, row: AiSettings | None, defaults: AiDefaults) -> AiSettingsView:
    if row is None:
        return AiSettingsView(
            customer_id=customer_id,
            is_enabled=defaults.is_enabled,
            base_url=defaults.base_url,
            model=defaults.model,
            timeout_seconds=defaults.timeout_seconds,
            has_api_key=bool(defaults.api_key),
            api_key_source="environment" if defaults.api_key else None,
            is_customized=False,
        )
    source: ApiKeySource | None
    if row.encrypted_api_key is not None:
        source = "custom"
    elif defaults.api_key:
        source = "environment"
    else:
        source = None
    return AiSettingsView(
        customer_id=customer_id,
        is_enabled=row.is_enabled,
        base_url=row.base_url,
        model=row.model,
        timeout_seconds=row.timeout_seconds,
        has_api_key=source is not None,
        api_key_source=source,
        is_customized=True,
    )


@dataclass
class ResolveAiConnection:
    ai_settings: AiSettingsRepository
    secrets_provider: SecretProvider
    defaults: AiDefaults

    async def execute(self, customer_id: UUID) -> AiConnection | None:
        """None means "AI is off for this customer" — channels then behave exactly as in
        Phase 1. Never raises for a missing configuration."""
        row = await self.ai_settings.get_by_customer(customer_id)
        if row is None:
            if not (self.defaults.is_enabled and self.defaults.base_url and self.defaults.api_key):
                return None
            return AiConnection(
                base_url=self.defaults.base_url,
                api_key=self.defaults.api_key,
                model=self.defaults.model,
                timeout_seconds=self.defaults.timeout_seconds,
            )
        if not row.is_enabled:
            return None
        api_key = (
            self.secrets_provider.decrypt(row.encrypted_api_key).decode("utf-8")
            if row.encrypted_api_key is not None
            else self.defaults.api_key
        )
        if not api_key:
            return None
        return AiConnection(
            base_url=row.base_url,
            api_key=api_key,
            model=row.model,
            timeout_seconds=row.timeout_seconds,
        )


@dataclass
class GetAiSettings:
    customers: CustomerRepository
    ai_settings: AiSettingsRepository
    defaults: AiDefaults

    async def execute(self, *, customer_id: UUID) -> AiSettingsView:
        if await self.customers.get(customer_id) is None:
            raise NotFoundError(f"customer {customer_id} not found")
        row = await self.ai_settings.get_by_customer(customer_id)
        return _view(customer_id, row, self.defaults)


@dataclass
class UpdateAiSettings:
    customers: CustomerRepository
    ai_settings: AiSettingsRepository
    secrets_provider: SecretProvider
    defaults: AiDefaults
    audit_log: AuditLogRepository
    clock: Clock

    async def execute(
        self,
        *,
        customer_id: UUID,
        is_enabled: bool,
        base_url: str,
        model: str,
        timeout_seconds: int,
        api_key: str | None,
        clear_api_key: bool,
        correlation_id: str,
    ) -> AiSettingsView:
        """`api_key=None` keeps whatever key is stored; a non-empty string replaces it;
        `clear_api_key=True` removes the stored key (falling back to the environment key)."""
        if await self.customers.get(customer_id) is None:
            raise NotFoundError(f"customer {customer_id} not found")

        base_url = base_url.strip().rstrip("/")
        model = model.strip()
        try:
            validate_ai_connection_fields(base_url, model, timeout_seconds)
        except InvalidAiSettingsError as exc:
            raise AiSettingsInvalidError(str(exc)) from exc

        now = self.clock.now()
        existing = await self.ai_settings.get_by_customer(customer_id)
        encrypted_key = existing.encrypted_api_key if existing is not None else None
        if clear_api_key:
            encrypted_key = None
        elif api_key is not None and api_key.strip():
            encrypted_key = self.secrets_provider.encrypt(api_key.strip().encode("utf-8"))

        if is_enabled and encrypted_key is None and not self.defaults.api_key:
            raise AiSettingsInvalidError("an API key is required to enable the AI assistant")

        row = AiSettings(
            id=existing.id if existing is not None else uuid4(),
            customer_id=customer_id,
            is_enabled=is_enabled,
            base_url=base_url,
            encrypted_api_key=encrypted_key,
            model=model,
            timeout_seconds=timeout_seconds,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        await self.ai_settings.save(row)
        await AuditWriter(self.audit_log, self.clock).write(
            customer_id=customer_id,
            prom_instance_id=None,
            installation_id=None,
            channel_type=None,
            channel_user_id=None,
            prom_user_id=None,
            action="ai_settings.updated",
            target_type="ai_settings",
            target_id=str(row.id),
            correlation_id=correlation_id,
            result_status=ResultStatus.SUCCESS,
            error_code=None,
        )
        return _view(customer_id, row, self.defaults)


@dataclass
class AiConnectionTestResult:
    is_ok: bool
    models: list[str]
    """Chat-capable model ids the gateway offers (all ids when it doesn't report modality)."""
    model_found: bool
    latency_ms: int | None
    sample_intent: str | None
    error_code: str | None
    error_message: str | None


@dataclass
class TestAiConnection:
    __test__ = False  # a use case, not a pytest test class (name starts with "Test")

    customers: CustomerRepository
    ai_settings: AiSettingsRepository
    secrets_provider: SecretProvider
    defaults: AiDefaults
    llm_client: LlmClient
    intent_parser: IntentParser
    clock: Clock

    async def execute(
        self,
        *,
        customer_id: UUID,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        api_key: str | None = None,
    ) -> AiConnectionTestResult:
        """Tests the given (unsaved) values, falling back to the saved/default ones for any
        omitted field — so the settings page can test before saving."""
        if await self.customers.get(customer_id) is None:
            raise NotFoundError(f"customer {customer_id} not found")
        row = await self.ai_settings.get_by_customer(customer_id)
        stored_key = (
            self.secrets_provider.decrypt(row.encrypted_api_key).decode("utf-8")
            if row is not None and row.encrypted_api_key is not None
            else self.defaults.api_key
        )
        connection_base = base_url or (row.base_url if row else self.defaults.base_url) or ""
        connection = AiConnection(
            base_url=connection_base.strip().rstrip("/"),
            api_key=(api_key or "").strip() or (stored_key or ""),
            model=(model or (row.model if row else self.defaults.model)).strip(),
            timeout_seconds=timeout_seconds
            or (row.timeout_seconds if row else self.defaults.timeout_seconds),
        )
        if not connection.base_url or not connection.api_key:
            return AiConnectionTestResult(
                is_ok=False,
                models=[],
                model_found=False,
                latency_ms=None,
                sample_intent=None,
                error_code=AiNotConfiguredError.error_code,
                error_message="base URL and API key are both required",
            )

        try:
            infos = await self.llm_client.list_models(connection)
        except DomainError as exc:
            return AiConnectionTestResult(
                is_ok=False,
                models=[],
                model_found=False,
                latency_ms=None,
                sample_intent=None,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        chat_models = [m.id for m in infos if m.is_chat]
        model_found = connection.model in {m.id for m in infos}

        started = time.monotonic()
        try:
            command = await self.intent_parser.parse(
                connection, _TEST_MESSAGE, today=self._today(), project_names=[]
            )
        except DomainError as exc:
            return AiConnectionTestResult(
                is_ok=False,
                models=chat_models,
                model_found=model_found,
                latency_ms=None,
                sample_intent=None,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        return AiConnectionTestResult(
            is_ok=True,
            models=chat_models,
            model_found=model_found,
            latency_ms=latency_ms,
            sample_intent=command.intent.value,
            error_code=None,
            error_message=None,
        )

    def _today(self) -> date:
        return self.clock.now().date()
