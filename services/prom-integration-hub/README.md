# prom-integration-hub

LINE/Discord Integration Hub for Prom (a Plane-based project management product). Modular
Monolith, Clean/Hexagonal Architecture. See `AGENTS.md` for the dependency-direction rule and
`/Users/patcharanatyamban/Desktop/NT/Prom/docs/architecture/00-gate0-1-discovery-and-plan.md` for
the full architecture background.

This is the **Gate 7 (Webhook, Queue, Notification)** state, on top of Gates 2-6's foundation:
customer/instance/workspace/channel-installation domain model, encrypted credential storage,
LINE/Discord credential validation, one-time-code account linking (rate limiting, tenant
isolation, audit logging), work-item CRUD/list/search against Prom's real REST API, a generic
Hub-side idempotency cache, a service-token-protected REST API for all of the above, the LINE
webhook (public, fail-closed HMAC signature verification, event dedup, Quick Reply rendering),
and the Discord Gateway bot (`apps/discord_bot/main.py`, reusing the same channel-agnostic
conversation state machine as LINE). Gate 7 closes the loop those channels were built waiting
for: a public, signature-verified, fail-closed inbound Prom (Plane) webhook route
(`POST /webhooks/v1/prom/{workspace_connection_id}`) that determines who to notify from real
verified webhook payload shapes (`apps/api/plane/bgtasks/webhook_task.py`, this session's
research), buffers notification-worthy events per `(linked_identity, work_item)` into a 45s
debounce window, sends one summarized message via the existing `SendPrivateNotification`
(built in Gate 5, unused until now), retries/dead-letters failed sends, and a Celery-beat
scheduler (`notifications.check_due_dates`, hourly, Asia/Bangkok-gated) that reuses Gate 4's
`ListMyWorkItems` as-is to notify on due-soon/due-today/overdue work items with same-day dedup.
Hub never persists work items itself — see "Work items (Gate 4)" below.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # edit values for local dev
```

## Run

```bash
# Port 8001, not the default 8000 -- Plane's own docker-compose-local.yml already uses 8000.
uvicorn apps.api.main:app --reload --port 8001
# GET http://localhost:8001/healthz  -> liveness, no dependencies
# GET http://localhost:8001/readyz   -> checks Postgres + Redis (needs them running)

celery -A apps.worker.main.celery_app worker --loglevel=info
celery -A apps.scheduler.main.celery_app beat --loglevel=info

python -m apps.discord_bot.main   # starts one discord.py Client per active Discord installation
```

Local infra (Postgres, Redis, api, worker, beat, discord-bot) via Docker Compose:

```bash
docker compose -f deployments/docker-compose.yml up
```

## Database migrations

```bash
alembic upgrade head      # apply migrations
alembic revision -m "add x"   # create a new migration
```

Migrations run against a **sync** (psycopg) connection derived from `DATABASE_URL`; the
application itself uses an **async** (asyncpg) engine. See `migrations/env.py`.

## Quality gates

```bash
ruff check .
ruff format --check .
mypy src apps
pytest -q              # unit tier only (default) — infra-free, always runs
lint-imports
```

## Integration tests (Gate 3+)

A second tier of tests exercises real Postgres/Redis (repository SQL, unique constraints, the
Redis-backed rate limiter, the idempotency-record table). Excluded from the default `pytest -q`
run via `addopts = "-m 'not integration'"` in `pyproject.toml`.

```bash
docker compose -f deployments/docker-compose.yml up -d postgres redis
alembic upgrade head
pytest -m integration
docker compose -f deployments/docker-compose.yml down -v   # tear down after
```

Postgres's host port is **55432**, not the default 5432 — see the comment in
`deployments/docker-compose.yml` (a conflict with something else already bound to 5432 on the
machine this was developed on; internal container-to-container traffic is unaffected). Adjust
`DATABASE_URL` in `.env`/your shell accordingly when running the app or tests against the
Docker-provided Postgres directly (as opposed to some other local Postgres on 5432).

## Work items (Gate 4)

Hub **never persists work items** — `src/domain/work_items/entities.py` has no repository port
and there is no SQLAlchemy model for it; every work-item use case resolves a delegated credential
for `(prom_instance_id, prom_user_id)` and calls Prom's REST API live on every request. The exact
endpoints/fields (`target_date` not `due_date`, state `group` values, the paginated `results`/
`next_cursor`/`next_page_results` envelope, the `external_id`/`external_source` 409-dedup
behavior on create) were verified against the pinned `plane` source, not guessed — see
`src/application/prom_api/ports.py`'s docstring and
`docs/architecture/00-gate0-1-discovery-and-plan.md`.

Routes (`/api/v1/work-items/...`, all behind `X-Hub-Service-Token` like every other route):
`POST /` (create), `GET /my-tasks` (assigned/due_today/overdue, client-side filtered — see
limitation below), `GET /search`, `PATCH /{id}/state`, `PATCH /{id}/assignees`,
`POST /{id}/comments`, `POST /{id}/complete`, `GET /projects`, `GET /projects/{id}/states`,
`GET /projects/{id}/members`, `GET /{id}` (detail — registered last on purpose, see the comment in
`apps/api/routers/work_items.py` about single-segment route ordering).

### Idempotency

Every write route accepts an optional `Idempotency-Key` header. Two independent layers exist —
not redundant, they cover different failure modes:

1. **Hub-side cache** (`idempotency_records` table, `apps/api/idempotency.py`): keyed by
   `(customer_id, route, idempotency_key)`; a repeat request with the same key and the same body
   returns the cached response without re-invoking the use case (or calling Prom) at all. A
   repeat with the same key but a *different* body returns `409 IDEMPOTENCY_KEY_CONFLICT`.
2. **Prom's own dedup** (`external_id`/`external_source` on work-item creation only): even if the
   Hub-side cache was bypassed, expired (24h TTL), or the caller never sent the header, Prom
   itself rejects a duplicate `(project, external_source, external_id)` with `409` and the
   existing item's id — `CreateWorkItem` treats that as a successful idempotent replay
   (`was_replay=True` in the response), not an error.

## Service-to-service auth

Every `/api/v1/*` route requires a `X-Hub-Service-Token` header matching `HUB_SERVICE_TOKEN` (see
`.env.example`) — this models the service-token half of Hybrid Authentication: these endpoints
are called by a trusted Prom Core component server-to-server, never directly by end users, LINE,
or Discord.

## LINE webhook (Gate 5)

`POST /webhooks/v1/line/{installation_id}` is the **only** public, unauthenticated-by-service-
token route in this codebase — LINE calls it directly. Its own auth is HMAC-SHA256 signature
verification (`src/channels/line/signature.py`) against the installation's `channel_secret`,
which **fails closed**: a missing/empty secret or missing `X-Line-Signature` header is rejected
(401), never treated as "skip verification." This is the fix for a real bug found in the
`Plane_Line` reference prototype during Gate 0 discovery (`base.py:40-47` there returned `True` —
verification passed — when the secret was empty). Installations must be created with both
`channel_access_token` *and* `channel_secret` in the `credentials` dict (`POST /api/v1/
installations`) for the webhook to work — the credential *validator* only needs the access token,
so `channel_secret` isn't otherwise enforced at creation time; if it's missing, every webhook call
correctly fails closed rather than silently working unsigned.

Every inbound LINE event is deduplicated via `webhookEventId` (falling back to a
`(user, message/postback, timestamp)` composite when absent) in Redis with a 24h TTL — LINE
redelivers webhooks, and Hub must not create or send anything twice as a result.

The conversation state machine (`src/application/work_items/conversation.py` —
`CreateWorkItemFlow`, `UpdateWorkItemFlow`) is deliberately channel-agnostic: it owns step
sequencing and calls Gate 4's use cases, and knows nothing about LINE Quick Replies or Flex
Messages. `src/channels/line/handler.py` (`LineWebhookHandler`) is the only LINE-specific piece
driving it — Gate 6's Discord adapter is expected to reuse the flow classes unchanged and write
its own controller/presenter. State is stored in Redis (`ConversationStateRepository`, 10-minute
TTL) keyed by `(channel_installation_id, channel_user_id)`.

Work-item web links use the verified route shape from the pinned `plane` source
(`apps/web/app/routes/core.ts:145-146`, layout entry `":workspaceSlug/projects/:projectId/
issues/:issueId"`): `{base_url}/{workspace_slug}/projects/{project_id}/issues/{work_item_id}` —
not guessed.

**No import-linter contract changes were needed for this gate.** `src/channels/line/` depends
only on `src/application` (ports + use cases + the conversation flows) and `src/domain`, injected
via constructor — the composition root (`apps/api/dependencies.py`) wires the concrete Redis/
Postgres/httpx adapters, same Hexagonal pattern as every use case in Gates 3-4. The existing
"channels -> application -> domain" layers contract already covered this.

**"Move project" (§8.4's "ย้าย Project") is not implemented and no new Gate 4 use case was added
for it** — `project` is a `read_only_field` on Plane's `IssueSerializer`
(`apps/api/plane/api/serializers/issue.py:72`, confirmed during Gate 0/4 research), so a work
item's project cannot be changed via Plane's public API PATCH endpoint at all. This is a genuine
platform limitation, not a Hub scope cut — flagged rather than silently dropped.

## Discord bot (Gate 6)

Unlike LINE, there is **no new public HTTP route** for Discord. Discord's HTTP "Interactions
Endpoint" model only ever delivers slash-command/component interactions — it cannot receive plain
DM text, which the master prompt's account-linking flow requires (§7, "ส่งรหัส ผ่าน private
chat/DM"). Plain DM text is only deliverable over Discord's **Gateway** (a persistent WebSocket),
so Gate 6 adds a dedicated long-running process instead: `apps/discord_bot/main.py`, run with
`python -m apps.discord_bot.main`, entirely separate from `apps/api` and `apps/worker`/`beat`.

At startup it queries every `ACTIVE` Discord `ChannelInstallation`
(`ChannelInstallationRepository.list_active_by_channel_type`, new in Gate 6, integration-tested
against real Postgres in `tests/integration/test_repositories.py`) and starts one `discord.py`
`Client` per installation, all running concurrently (`asyncio.gather`). **It does not pick up
installations created, activated, or revoked after startup** — restart the process to pick up
changes. This is a deliberate Phase 1 scope cut (no hot-reload), not an oversight.

**Discord bot setup — required Developer Portal step**: the bot's Discord Application must have
**Message Content Intent** enabled (Bot settings → Privileged Gateway Intents) or every DM's
`content` field arrives empty regardless of what the user typed. This is a Discord-enforced
privileged intent, not something Hub can work around in code.

**Architecture**: `src/channels/discord/handler.py` (`DiscordWebhookHandler`) is the Discord
counterpart to `LineWebhookHandler` — same responsibilities, same channel-agnostic
`CreateWorkItemFlow`/`UpdateWorkItemFlow` from `src/application/work_items/conversation.py`
**reused completely unchanged**, proving out the design LINE's README predicted. The only new
discord.py-touching code is `src/channels/discord/gateway_client.py` (converts real
`discord.Message`/`Interaction` objects into a channel-agnostic `DiscordEvent` + a bound `send`
closure, and turns `DiscordMessage` output back into real `discord.ui.View`s) — everything else,
including the handler itself, is unit-tested with plain dataclasses and duck-typed fakes, no live
Gateway connection needed (`tests/channels/test_discord_handler.py`,
`tests/channels/test_discord_gateway_client.py`).

**Presenter divergence from LINE, deliberate**: LINE has no native "pick one of N" component, so
Gate 5 uses Quick Reply buttons for every choice. Discord has a native Select Menu (up to 25
options), which is the more idiomatic fit — `src/channels/discord/components.py` uses Select Menus
for project/state/assignee/update-action pickers and plain Buttons only for fixed, parameterless
actions (confirm/cancel/menu). Because every picker's chosen value travels back via the
interaction's own payload rather than being encoded into `custom_id` (unlike LINE's
`postback.py`), Discord's `custom_id`s never need LINE-style query-string encoding — the one
exception is the my-tasks Select, whose options need to carry *two* ids (project + work item) in
Select's single `value` field, so those are joined as `"{project_id}:{work_item_id}"` and split on
the other end (see `build_my_tasks_list`'s docstring).

**Event dedup**: same `WebhookDedupStore` port and Redis adapter as LINE, keyed by Discord's
message/interaction id with a 24h TTL — judged worth keeping even though Gateway redelivery is
rarer than LINE's HTTP webhook retries, since it's the same port, same cost, and the master
prompt's replay-protection requirement (§11) doesn't carve out an exception for lower-probability
transports.

**Component integrity**: a stale button/select tap (conversation state already expired, or the
bot process restarted and the View's callback closures are gone) gets a graceful "session expired,
start over" reply rather than a crash or silent no-op — see
`DiscordWebhookHandler._handle_component`'s `state is None` branch and
`test_stale_component_with_no_conversation_state_replies_gracefully`.

**No import-linter contract changes were needed.** `discord` was already in the domain/application
forbidden-modules list since Gate 2 (added defensively before any Discord code existed);
`src/channels/discord/` depends only on `src/application` + `src/domain` (constructor-injected),
same as `src/channels/line/` — the existing "channels -> application -> domain" layers contract
already covered it.

## Notifications (Gate 7)

**Inbound webhook**: `POST /webhooks/v1/prom/{workspace_connection_id}` — public, not behind
`X-Hub-Service-Token` (its own auth is the `X-Plane-Signature` HMAC check, fail-closed on a
missing/unconfigured `WorkspaceConnection.webhook_secret_encrypted`, same principle as LINE's
signature check). Plane has **no public-API way to register a webhook itself** (verified:
`apps/api/plane/api/` has zero webhook endpoints — webhook CRUD only exists in the
session-authenticated `plane.app`), so setting one up is a manual, one-time admin step:

1. An admin creates a webhook in Plane's own web UI settings, pointed at
   `GET /api/v1/workspace-connections/{id}` (the response's `webhook_url` field tells you the
   exact URL to paste, computed from `HUB_PUBLIC_BASE_URL`).
2. `PATCH /api/v1/workspace-connections/{id}/webhook-secret` (service-token protected) with the
   `secret_key` Plane generated — encrypted at rest (Fernet), never echoed back.

**Determining who to notify**: `determine_notifications()` (pure function,
`src/application/notifications/use_cases.py`) maps a verified Plane webhook payload to
`(work_item_id, [(prom_user_id, event_type)])` — assignee-added/removed from diffing
`activity.old_value`/`new_value` on an `assignees` field-change, state-changed/completed from an
`activity.field == "state"` change (checking the new state's `group`), comment notifications via
a live `GetWorkItem` lookup (the comment payload itself doesn't carry the parent issue's
assignees). Every affected `prom_user_id` is resolved to **every** active `LinkedIdentity`
across **all** their linked channels (a user linked on both LINE and Discord gets notified on
both), filtered by per-`(identity, event_type)` `NotificationPreference` (default-on, opt-out).

**Debounce**: same-`(linked_identity, work_item)` notifications within a 45s window
(`_DEBOUNCE_WINDOW_SECONDS`) collapse into one `OutboundMessageRecord`, batching
`event_types_batched` — one summarized message sent instead of one per field change (Plane fires
one webhook delivery per changed field on a multi-field PATCH). `FlushOutboundMessage` (the
Celery-scheduled send) composes and sends via the existing `SendPrivateNotification`, incrementing
`attempt_count` and re-raising on delivery failure so Celery's `autoretry_for`/`retry_backoff`
handles backoff; after `max_retries` the task dead-letters the record (`notification_tasks.py`) —
the applic­ation layer itself never dead-letters, only Celery-level retry-exhaustion does.

**Scheduled due-date checks**: `notifications.check_due_dates` (Celery beat, hourly, see
`apps/scheduler/main.py`) only acts when Asia/Bangkok's local hour matches `_DUE_CHECK_LOCAL_HOUR`
— the simplest way to get "once a day, at a sensible morning time" from a plain hourly interval
without a timezone-aware cron dependency. Reuses Gate 4's `ListMyWorkItems` as-is (same caps) for
`due_soon`/`due_today`/`overdue`, gated by the same `WebhookDedupStore` Redis port LINE/Discord
webhook dedup already uses (different key namespace) so multiple beat runs in one day don't repeat
a notification.

## Known limitations (Gate 4)

- **"My tasks" (assigned/due_today/overdue) is client-side filtered, with hard caps** — Plane's
  public API has no server-side assignee/due-date filter on this edition (`pql`/`filters` query
  params return HTTP 400, "filter results client-side" — verified against
  `apps/api/plane/api/views/issue.py`). `ListMyWorkItems` lists at most 10 projects per call
  (extras silently dropped, `truncated: bool` on the response tells the caller) and at most 3
  pages per project (100 items/page). This is a deliberate, documented Phase 1 tradeoff, not an
  oversight — a workspace with many large projects will not get complete results from this
  endpoint. See `src/application/work_items/use_cases.py` module docstring.
- **`list_projects`/`list_states`/`list_project_members` fetch only the first page** (100 items) —
  unlike `list_work_items`, these don't walk multiple pages. A workspace with >100 projects, or a
  project with >100 states/members, would be truncated. See `_LIST_PAGE_CAP` in
  `src/adapters/prom_api/client.py`.
- **Work-item JSON field names (`project`, `state`, etc.) are inferred from Django/DRF model
  field names, not captured from a live HTTP response** — high confidence (DRF's default
  ModelSerializer behavior is well-documented and deterministic), but flagged as a residual
  verification gap. Worth a contract test against a real Plane instance before Gate 5/6 depend on
  this in production — see the module docstring in `src/adapters/prom_api/client.py`.
- `/readyz` is not covered by the automated test suite — it requires a live Postgres and Redis.
  Verify manually against `docker compose -f deployments/docker-compose.yml up`.
- API tokens minted via `StartAccountLinking` (and Plane's own `/api/users/api-tokens/`) are
  **not workspace-scoped** — accepted as a documented Phase 1 tradeoff, see
  `docs/architecture/00-gate0-1-discovery-and-plan.md` §2.4/§5.
- `PromInstance`'s `https://`/non-local `base_url` invariant is a string check only (mirrors
  Plane's own webhook URL validator) — no DNS resolution, so it is not real SSRF protection.
- ~~Celery notification tasks are not implemented yet~~ — shipped in Gate 7, see "Notifications
  (Gate 7)" below.

## Known limitations (Gate 5)

- **No "set default workspace" command.** If a `LinkedIdentity.default_workspace_connection_id`
  isn't set and the installation has more than one linked workspace, every `create`/`my_tasks`/
  `update` action asks the user to pick a workspace for that session only — the choice is never
  persisted. Building a persistent "change my default workspace" command is out of Gate 5 scope
  (noted in the master prompt's own linking-flow description as a "can pick a default later"
  follow-up, not a Phase 1 requirement).
- **Picker caps**: project picker at create-flow Title step is capped at 10 (`_PROJECT_PICKER_CAP`
  in `conversation.py`), and every LINE Quick Reply is capped at LINE's own 13-item/20-char-label
  limits (`_QUICK_REPLY_MAX_ITEMS`/`_LABEL_MAX_LEN` in `messages.py`) — a workspace with more
  projects, or a project with more states/members, won't show all of them in a picker. Same
  "documented tradeoff, not an oversight" reasoning as Gate 4's my-tasks caps.
- **Comment doesn't require a confirmation step**, unlike state-change/reassign/mark-complete — a
  deliberate choice: adding a comment doesn't change the work item's state or ownership, and the
  master prompt's confirmation requirement (§8.4) is scoped to significant-effect writes.
- **The webhook route processes events synchronously within the request** (no queue/background
  task) — a webhook that triggers several Prom API calls in one turn (e.g. a full create-flow
  confirm) adds their latency directly to LINE's webhook response time. LINE's own timeout is
  generous (several seconds), and Phase 1 has no evidence this is a real problem yet, but it's
  worth watching once Gate 7's Celery worker exists — the same pattern could move here too.
  `apps/api/routers/line_webhook.py` always returns before doing any heavy work only in the sense
  that the reply the *user* sees per step is itself the webhook response; there is no separate
  "ack fast, process later" split in Gate 5.
- Reply-vs-push: every reply in this gate uses LINE's `reply_message` (bound to the triggering
  event's `reply_token`), never `push_message`, since everything happens within one webhook
  turn — `push_message` exists (`src/channels/line/client.py`) and is what Gate 7's
  `SendPrivateNotification`/`FlushOutboundMessage` actually call now that a real trigger
  (the Prom webhook) exists.
- Free text sent at a step that expects a button tap (e.g. typing during the project picker) gets
  a generic "please choose from the options above" nudge rather than a redraw of the exact
  picker — redrawing would need re-fetching/re-caching the picker's list, judged not worth the
  complexity for Phase 1.
- ~~Adjacent finding from Gate 6: LINE's main menu Quick Reply buttons were a silent no-op~~ —
  fixed post-Gate-6: `LineWebhookHandler._handle_postback` now routes a bare
  `action in {"create","my_tasks","update"}` postback through the same `_start_or_defer` helper
  the typed keywords (`สร้างงาน`/`งานของฉัน`/`อัปเดต`) already used. See
  `tests/channels/test_line_handler.py::test_tapping_menu_quick_reply_buttons_routes_same_as_typed_keywords`.
  Gate 6's Discord menu never had this gap in the first place (components-first design).

## Known limitations (Gate 6)

- **No hot-reload of Discord installations** — `apps/discord_bot/main.py` enumerates active
  installations once at startup; creating, activating, or revoking a Discord installation requires
  restarting the process to take effect. Documented, not a bug.
- **Message Content Intent must be manually enabled per Discord Application** in the Developer
  Portal (a privileged Gateway intent) — there is no way for Hub to detect or work around this from
  code; if it's off, every DM's text arrives empty and the bot will appear completely unresponsive
  to typed input (buttons/selects still work, since their data doesn't depend on this intent).
- **`apps/discord_bot/main.py` itself is not exercised by the automated test suite** — its
  `client.start(token)` calls need a real Gateway connection, which is untestable without live
  Discord infrastructure (same category of gap as `/readyz` since Gate 3, and Gate 5's
  synchronous-webhook-processing choice). Everything it wires together
  (`_make_handler_factory`'s use-case construction, `list_active_by_channel_type`,
  `build_client`'s event routing) is independently tested; only the outermost `asyncio.gather` /
  `Client.start` loop itself is not.
- **Per-event DB session is not wrapped in an explicit `async with`** in
  `_make_handler_factory` — unlike `apps/api`'s FastAPI dependency (`get_session`), which closes
  its session deterministically via a context manager per request, discord.py's callback shape
  (fire-and-forget event handlers, no natural "request scope" to hang a context manager off)
  makes that awkward to express cleanly for Gate 6. Each session is still short-lived (one event's
  worth of work) and gets garbage-collected/pooled normally, but this is noted as a rough edge
  worth revisiting rather than a fully clean implementation — see the comment in
  `_make_handler_factory`.
- **View components are not `client.add_view()`-registered** — they're built fresh per outgoing
  message with callbacks as Python closures, which only work for the lifetime of the process that
  sent them. A bot restart mid-conversation leaves any button/select still visible in the user's
  Discord client non-functional (the tap will time out) rather than resuming the flow. Consistent
  with the "no hot-reload" limitation and the conversation state's own 10-minute TTL — a narrow
  window, not a durability guarantee this gate makes.
- **DM delivery is not guaranteed** — Discord allows users to disable DMs from bots/non-friends;
  Hub cannot detect or override this. `DiscordMessagePublisher`/`gateway_client.py`'s send paths
  will raise `ChannelDeliveryFailedError` on such a rejection, same handling as any other delivery
  failure, but there is no separate user-facing guidance for "you have DMs closed" — a genuine
  Discord platform constraint, not a Hub bug.
- **"Move project" is still unsupported**, same platform-level reason as Gate 5 (`project` is
  read-only on Plane's `IssueSerializer`) — not re-verified here, just carried forward.

## Known limitations (Gate 7)

- **True @-mention parsing is not implemented.** Plane's `IssueComment` model has no structured
  mentions field (verified — grepped `apps/api/plane/db/models/issue.py`); mentions are embedded
  in `comment_html`/`comment_json` rich-text markup whose exact structure was not verified this
  session. Comment notifications go to the work item's **current assignees only**
  (`ProcessPromWebhookEvent._resolve_comment_assignees`), not to whoever was actually @-mentioned
  — a named, documented gap, not a silently-dropped requirement.
- **Registering a Plane webhook is a manual admin step**, not something Hub can self-provision —
  see "Notifications (Gate 7)" above. This is a real operational step every customer workspace
  needs, not an oversight.
- **Default-only Asia/Bangkok timezone** for the due-date scheduler — no per-customer/per-user
  timezone preference system, per the master prompt's own explicit Phase 1 scope instruction.
- **The scheduler's local-morning-time gate is hour-precision, not minute-precision** — the beat
  task runs hourly and only acts when Asia/Bangkok's local hour matches `_DUE_CHECK_LOCAL_HOUR`;
  a customer expecting a notification at a specific minute won't get that precision. Trades a
  timezone-aware cron dependency for this simplicity.
- **Retry/dead-letter scheduling itself (Celery's `autoretry_for`/`retry_backoff`/`max_retries`)
  is infrastructure glue not exercised by the automated test suite** — untestable without a live
  broker. What IS tested: the application-layer logic Celery's retry wraps
  (`FlushOutboundMessage` raising on failure, incrementing `attempt_count`, succeeding on a
  subsequent call) — see `tests/application/test_notifications.py`. The dead-letter transition
  itself lives in `notification_tasks.py`'s exception handling around `self.request.retries >=
  self.max_retries`, verified by code reading, not by an automated test that exhausts real Celery
  retries.
- **`FlushOutboundMessage`'s composed message uses the raw Prom `work_item_id`**, not a nicer
  `{project_identifier}-{sequence_id}: {title}` summary — that would need another delegated-
  credential lookup + `GetWorkItem` call per send; deferred as purely cosmetic (see the class
  docstring in `use_cases.py`).
- **`NotificationPreferenceRepository.set_enabled` is select-then-insert-or-update, not
  SAVEPOINT-wrapped** against its `(linked_identity_id, event_type)` unique constraint — unlike
  `LinkedIdentityRepository`/`WebhookEventRepository`/`IdempotencyRepository`. A genuinely
  concurrent double-toggle of the same preference from the same user is low-probability and
  low-consequence (a UI double-click, not a security boundary); flagged rather than silently
  matched to the SAVEPOINT pattern used elsewhere.
