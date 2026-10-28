# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Prom is a project-management product built on open-source [Plane](https://plane.so), plus a
separate **Prom Integration Hub** that connects it to LINE and Discord. Two codebases live here:

- `services/prom-integration-hub/` — the new product (Python 3.11, FastAPI, Celery, discord.py).
  Most work happens here.
- `plane/` — a git submodule pointing at the fork `Pacharanut/plane`, branch `prom/patches`.
  Treat it as **read-only** except for the documented patches in `core_patches/`.

Clone with `git clone --recurse-submodules`, or run `git submodule update --init --recursive`.

## Hub commands (run from `services/prom-integration-hub/`)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

# Quality gates — run all of these before calling work done
ruff check .
ruff format --check .
mypy src apps                # strict mode
pytest -q                    # unit tier only; integration tests are excluded by default
lint-imports                 # architecture contracts (also run by tests/architecture/)

# Single test
pytest tests/channels/test_line_handler.py::test_name -q

# Integration tier (real Postgres + Redis)
docker compose -f deployments/docker-compose.yml up -d postgres redis
alembic upgrade head
pytest -m integration
docker compose -f deployments/docker-compose.yml down -v

# Run processes natively
uvicorn apps.api.main:app --reload --port 8001
celery -A apps.worker.main.celery_app worker --loglevel=info
celery -A apps.scheduler.main.celery_app beat --loglevel=info
python -m apps.discord_bot.main

# Migrations (Alembic uses a sync psycopg URL derived from DATABASE_URL; the app uses asyncpg)
alembic revision -m "add x"
```

Host ports differ from the defaults to avoid clashing with Plane: Hub API **8001**, Hub
Postgres **55432**, Hub Redis **6380**. `tests/conftest.py` defaults `REDIS_URL` to `:6379`, so
set `REDIS_URL=redis://localhost:6380/0` (and DB URL to `:55432`) when running integration tests
against the compose stack.

## Hub architecture

Modular monolith with Clean/Hexagonal layering, **enforced by import-linter**
(`pyproject.toml` `[tool.importlinter]`; see `services/prom-integration-hub/AGENTS.md`):

```
src/channels (LINE/Discord controllers + presenters)
  -> src/application (use cases, ports)
    -> src/domain (entities, ports)
       <- src/adapters, src/infrastructure (implement ports)
apps/{api,worker,scheduler,discord_bot}  = composition roots; the only place allowed to import everything
```

- `src/domain` and `src/application` must never import FastAPI, Starlette, SQLAlchemy, httpx,
  Redis, Celery, `discord`, `linebot`, `boto3`, etc. Pydantic is allowed.
- Wiring of concrete adapters into use cases happens in `apps/api/dependencies.py` (and the
  equivalent factories in worker/scheduler/discord_bot).
- Config flows only through `src.infrastructure.configuration.Settings`; no global mutable state.
- Controllers stay thin; business logic goes in `src/application` use cases.
- External calls need timeouts; never retry validation/auth/permission errors.
- AI (Phase 2) lives only in `src/application/ai/` and `src/adapters/ai/`: the LLM turns text into a
  `ParsedCommand`, deterministic code resolves ids/dates, and every write is confirmed first and runs
  through the existing use cases. Settings are per customer, edited in Plane's Settings → AI Assistant.
  See `docs/architecture/04-adr-001-ai-intent-layer.md`.

Key cross-cutting design points:

- **Hub never persists work items.** Every work-item use case resolves a delegated credential for
  `(prom_instance_id, prom_user_id)` and calls Plane's REST API live
  (`src/adapters/prom_api/client.py`, ports in `src/application/prom_api/ports.py`).
- **Auth surfaces:** all `/api/v1/*` routes require `X-Hub-Service-Token` (server-to-server only).
  The public routes are the webhooks, each with its own fail-closed HMAC check:
  `POST /webhooks/v1/line/{installation_id}` (`X-Line-Signature`) and
  `POST /webhooks/v1/prom/{workspace_connection_id}` (`X-Plane-Signature`). A missing secret must
  reject the request, never skip verification.
- **Channel-agnostic conversation flows:** `src/application/work_items/conversation.py`
  (`CreateWorkItemFlow`, `UpdateWorkItemFlow`) owns step sequencing; LINE
  (`src/channels/line/handler.py`) and Discord (`src/channels/discord/handler.py`) are separate
  controller/presenter pairs driving the same flows. Conversation state lives in Redis (10-min TTL).
- **Discord uses the Gateway, not HTTP interactions** (needed for plain DM text). The bot
  process starts one client per active Discord installation **at startup only**; restart it to
  pick up changes. It exits when there are zero installations.
- **Event dedup** (LINE/Discord/due-date scheduler) shares the `WebhookDedupStore` Redis port, 24h TTL.
- **Idempotency** has two layers: the Hub-side `idempotency_records` cache keyed by
  `Idempotency-Key` (`apps/api/idempotency.py`), and Plane's own `external_id`/`external_source`
  409 dedup on work-item create (treated as a successful replay).
- **Notifications:** Plane webhook → `determine_notifications()` (pure function in
  `src/application/notifications/use_cases.py`) → 45s debounce per `(linked_identity, work_item)`
  into `OutboundMessageRecord` → Celery `FlushOutboundMessage` with retry/dead-letter
  (`notification_tasks.py`). Due-date checks run hourly via beat, gated to one Asia/Bangkok hour.
- Plane API details (field names like `target_date`, `activity.field` values, past-tense webhook
  actions, pagination envelope, route shapes) were verified against the pinned `plane/` source.
  When changing them, grep `plane/apps/api/plane/` to confirm instead of guessing.

## Naming conventions (Hub)

- Interfaces are plain (`PromClient`, `MessagePublisher`); implementations name their technology
  (`HttpPromClient`, `PostgresIdentityRepository`, `LineMessagePublisher`).
- Canonical domain terms: `work_item` (never `task`/`issue`), `state` (never `column`),
  `customer`, `prom_instance`, `workspace`, `project`, `channel_installation`,
  `linked_identity`, `linking_code`.
- Booleans are prefixed `is_`/`has_`/`can_`/`should_`.

## Changing `plane/`

Plane has no plugin/extension seam on either backend or frontend, so the only changes to it are
isolated patches documented in `core_patches/`:

- `0001-prom-bridge` — new Django app `apps/api/plane/prom_bridge/` that relays browser calls to
  the Hub so the service token stays server-side (the browser must never call the Hub directly).
- `0002-prom-integration-ui` — workspace Integrations and profile Connected Accounts settings pages.

Patch commits go on the `prom/patches` branch inside the `plane/` submodule, then the submodule
pointer is bumped in this repo. A new patch needs its own `core_patches/NNNN-*/README.md`
covering everything listed in `core_patches/README.md` (upstream commit, why an API/add-on
approach wasn't enough, affected files, compatibility test, apply/rollback, owner) plus an ADR.
`plane/` has its own `AGENTS.md` for work inside it.

## Running Plane + Hub together

Follow `docs/operations/local-dev-testing.md` exactly; it records ports and workarounds that were
tested on this machine (Plane web on :3010, Plane Postgres on :5433 via an untracked override
file, an HTTPS nginx proxy on :8443 because `PromInstance.base_url` must be `https://` and
non-local, `django-debug-toolbar` re-installed into `plane-api-1` after each container recreate).
Don't touch the separate `plane-app` stack on port 3000.

## Docs

`docs/architecture/` holds the discovery/decision record (`00-…`), threat model, verification
pass, and Phase 1 definition of done. `docs/operations/runbook.md` covers operational procedures.
The Hub README lists known limitations by gate; check it before treating a gap as a bug.
