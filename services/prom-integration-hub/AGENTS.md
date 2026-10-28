# AGENTS.md — prom-integration-hub

## Dependency direction (enforced by import-linter, see `pyproject.toml` `[tool.importlinter]`)

```
Channel Controller (src/channels)
    -> Application Use Case (src/application)
        -> Domain (src/domain)
        -> Port/Interface (defined in src/domain or src/application)
            -> Adapter/Infrastructure (src/adapters, src/infrastructure)
```

- `src/domain` and `src/application` must **never** import FastAPI, Starlette, SQLAlchemy, httpx,
  requests, aiohttp, Redis, Celery, `discord`, `linebot`, or `boto3`. Pydantic is allowed there —
  it is a data-modeling library, not an infrastructure framework.
- `src/adapters` and `src/infrastructure` implement ports declared in `src/domain`/
  `src/application`; they may import domain/application, never the reverse.
- `src/channels` (LINE/Discord controllers and presenters) depend on `src/application` only.
- Composition/wiring (constructing adapters and injecting them into use cases) happens in
  `apps/api`, `apps/worker`, `apps/scheduler` — the only layer allowed to import everything.

Run `lint-imports` to verify these contracts; it also runs inside
`tests/architecture/test_import_boundaries.py`.

## Naming conventions

- Modules/functions/variables: `snake_case`. Classes/types: `PascalCase`. Constants:
  `UPPER_SNAKE_CASE`. Booleans: `is_`, `has_`, `can_`, `should_` prefix. Private implementation:
  `_leading_underscore`. Every public function has type hints.
- Interfaces are named plainly (`PromClient`, `IdentityRepository`, `MessagePublisher`,
  `SecretProvider`); implementations name their technology (`HttpPromClient`,
  `PostgresIdentityRepository`, `RedisConversationStore`, `LineMessagePublisher`,
  `DiscordMessagePublisher`). No vague names (`data`, `item`, `obj`, `temp`, `helper`, `manager`)
  without a qualifier.
- Canonical domain terms: `work_item` (never `task`/`issue`), `state` (never `column`),
  `customer`, `prom_instance`, `workspace`, `project`, `channel_installation`, `linked_identity`,
  `linking_code`.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
ruff format --check .
mypy src apps
pytest -q
lint-imports
docker compose -f deployments/docker-compose.yml config   # validate compose file only
```

## Rules

- Controllers are thin — no business logic. Business logic lives in `src/application` use cases.
- No global mutable state; configuration flows through `src.infrastructure.configuration.Settings`.
- No bare `except Exception: pass` — readiness/health checks are the only place that catches
  broadly, and they always report the failure rather than swallowing it.
- External calls (Prom API, LINE, Discord) must have timeouts; only retry errors that are
  actually retryable (never validation/authentication/permission errors).
- AI/LLM code (Phase 2, see `docs/architecture/04-adr-001-ai-intent-layer.md`) lives only in
  `src/application/ai/` (framework-free: prompt, command schema, matching, dates, assistant) and
  `src/adapters/ai/` (the OpenAI-compatible HTTP client). The LLM only turns text into a
  `ParsedCommand`; it never produces ids and never writes. Every write goes through a
  confirmation step and the existing work-item use cases. No vendor LLM SDKs — plain `httpx`.
