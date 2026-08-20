# Prom Platform & Integration Hub — Gate 0/1 Discovery & Architecture Plan

> Status: Approved (Product Owner authorized entry into Gate 2 on 2026-08-18)
> Scope: Gate 0 (Repository Discovery, read-only) + Gate 1 (Architecture & Implementation Plan)
> No source code was created, edited, moved, or deleted during Gate 0/1.

---

## 1. Understanding

Build **Prom** = Plane (upstream, read-only) + **Prom Integration Hub** (new system, Modular
Monolith / Clean-Hexagonal Architecture) connecting LINE/Discord via Plane's REST API and
Webhooks only. `Plane_Line` and `Plane-Discord-middleware` are historical reference only — never
a development base. Phase 1 has no LLM/AI features.

---

## 2. Repository Findings

### 2.1 `plane` (upstream, authoritative)

| Topic | Fact | Evidence |
|---|---|---|
| Version/commit | HEAD `e056bbf9eb`, branch `preview`, all `package.json` report `1.4.2`, no tag points exactly at HEAD (`git describe` → `v1.4.1-rc2-2-ge056bbf9eb`) | `package.json:2`, git log |
| Build system | pnpm monorepo (Turborepo), Django backend separate | `AGENTS.md` |
| Topology | `apps/web` (React Router v7, :3000), `apps/admin` (:3001 god-mode), `apps/space` (:3002), `apps/live` (Node/TS realtime editor server, Hocuspocus+Redis), `apps/api` (Django) | package.json per app |
| Plugin/Add-on mechanism | **Not found** — grep `plugin\|addon\|marketplace\|extension.?point` across `apps/api/plane/**/*.py` returned 0 matches for all terms. `INSTALLED_APPS` is a static hardcoded list. No Odoo-style inheritance/add-on system exists. | `apps/api/plane/settings/common.py:97-118` |
| Outbound Webhook | Real feature — workspace-scoped, `secret_key` auto-generated (`plane_wh_<hex>`), HTTPS-only + no-localhost validator, boolean flags per event category (`project`,`issue`,`module`,`cycle`,`issue_comment`), delivery-log endpoint | `apps/api/plane/db/models/webhook.py:33-46`, `apps/api/plane/app/urls/webhook.py:15-29`, `apps/api/plane/bgtasks/webhook_task.py` |
| Inbound webhook receiver | Not found — outbound only | (same search) |
| Public REST API | `plane.api` Django app, `X-Api-Key` auth via `APIKeyAuthentication`, `APIToken` model (`plane_api_<hex>`, per-user, optional workspace scope field, `is_service` flag, `expired_at`, default rate limit 60/min) | `apps/api/plane/api/middleware/api_authentication.py:16-35`, `apps/api/plane/db/models/api.py:23-42` |
| OAuth | Sign-in providers only (google/github/gitlab/gitea) — not a third-party app-installation/marketplace concept | `apps/api/plane/authentication/provider/oauth/` |
| License | AGPL-3.0-only, consistent SPDX headers in sampled files | `LICENSE.txt`, `COPYRIGHT.txt` |
| Deployment | `docker-compose.yml`: 12 services — web/admin/space/api/worker/beat-worker/migrator/live/plane-db(Postgres)/plane-redis/plane-mq(RabbitMQ)/plane-minio(S3)/proxy | `docker-compose.yml:2-166` |

Caveats not fully closed in Gate 0: frontend TS not exhaustively grepped for a theming/plugin
registry beyond the branding investigation in §2.4; `apps/api/.env.example` CORS/secret vars not
fully enumerated.

### 2.2 `Plane_Line` (LINE prototype — reference only)

- Full fork of Plane; LINE feature added as one large commit `e87a3c000` (+3831/-0, 25 files, dated
  2026-05-24). No upstream remote configured to cross-check base version.
- `LINE_SETUP.md`'s claimed file structure verified accurate against `git show --stat`.
- **LINE models embedded directly in Plane Django core**: `UserLineProfile`, `LineConnectToken`,
  `LineProjectSync` subclass `plane.db.models.base.BaseModel`, registered in
  `plane.db.models.__init__.py` alongside `Issue`/`Workspace`, direct FKs into core tables — violates
  every isolation requirement in this project's rules (`db/models/integration/line.py:14-111`,
  `db/models/__init__.py:22-33`).
- Core notification function patched inline (not event-driven): `bgtasks/notification_task.py`
  diff inserts a 22-line block directly inside `notifications()`.
- Redis conversation state reuses Plane core's Redis instance, no isolation
  (`views/line/conversation.py:1-31`, TTL 600s).
- 6-digit linking code generated with `random.choices` (not a CSPRNG), no rate-limit/lockout
  (`db/models/integration/line.py:40-41`, `views/line/base.py:176-208`).
- Webhook signature verification present (HMAC-SHA256, correct `compare_digest`) but **fails open**
  when the secret is unset (`views/line/base.py:40-47`) — real security bug.
- `LineProjectSync.channel_access_token` stored **plaintext** in the database.
- **Zero test coverage** for the LINE feature.
- No AI/LLM code.

🔴 **Critical security finding (reported live during Gate 0, unresolved as of this document):**
`plane-selfhost/plane-app/plane.env` contains what appear to be real, non-placeholder secrets
(`LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `SECRET_KEY`, weak DB passwords) committed to
the **public** GitHub repo `Pacharanut/Plane_Line` since 2026-05-24. The user was advised to rotate
these credentials if real; this is tracked as an open action item outside Gate 0/1 scope (see §12).

### 2.3 `Plane-Discord-middleware` (Discord prototype — reference only)

- Real stack (`requirements.txt`): `discord.py`, `fastapi`, `httpx`, `psycopg` (Postgres),
  `cryptography`, `openai`, `mcp[cli]`. **No Redis, no queue framework** (celery/dramatiq/rq = 0
  hits) — single-process app (`main.py:22-55`). This **conflicts** with the master prompt's claim of
  "Redis/Background processing" for this prototype — flagged, not resolved by guessing.
- Password-based login → API token: via a web page (Fernet-encrypted TTL state token links
  Discord↔login page), password used transiently and not persisted, resulting API token *is*
  persisted encrypted with Fernet in Postgres (`auth_service.py:34-144`, `auth_store.py:80-128`).
  Forbidden pattern for Prom regardless of the encryption-at-rest.
- `plane_client.py` (816 lines, ~35 async methods covering work items/projects/cycles/modules/
  comments/attachments, with retry/backoff) — good reference shape for a future `PromClient` port.
- `webhook_server/` (1704 lines) is inbound-only (Plane→Discord), correct HMAC verification (no
  fail-open bug found), includes a `NotificationAggregator` for debounce.
- AI/LLM code concentrated in `discord_bot/ai/` + `intent_parser/` (~3600 lines) but **leaks** into
  files outside that boundary (`resources/work_items/common.py`, `config.py`,
  `webhook_server/labels.py` have grep hits) — must verify precise import boundaries before treating
  any of those files as safe reference.
- 644 test functions across ~29 files; `plane_client` tests use `httpx.MockTransport` — good
  Contract Test pattern reference.
- `mcp_server.py` (349 lines) is a genuine MCP server with a `confirm=true` guard before writes —
  a Phase 2 AI extension-point idea, not part of Phase 1.

### 2.4 Follow-up investigations (post Gate 1 draft, before Gate 2 start)

**Delegated auth feasibility (no password ever collected by Hub/LINE/Discord):** Confirmed
feasible without a core patch. `/api/users/api-tokens/` (`ApiTokenEndpoint`,
`apps/api/plane/app/views/api.py:20-39`) creates a token from `request.user` using only the
existing session (`BaseSessionAuthentication`, `permission_classes=[IsAuthenticated]`) — no
password re-check. The frontend `create-token-modal.tsx` has no re-auth step either. CSRF is
explicitly disabled for this endpoint (`authentication/session.py:9-11`). **Gap:** the token
serializer marks `workspace` as read-only (`app/serializers/api.py:12-26`) — the client cannot
request a workspace-scoped token; tokens are always full-account scope. Decision: **accepted as a
Phase 1 tradeoff** (Plane's own per-request permission check remains the real authorization
backstop; Hub's `workspace_connections` mapping constrains what Hub actually attempts). Revisit as
a small extension/new endpoint in a later phase if stricter least-privilege is required.

**Rebranding feasibility (Plane → Prom, full rebrand required in Phase 1):** Classified as
**(b) mostly config-driven** — not a broad core patch. Title/meta centralized in
`packages/constants/src/metadata.ts:7,17` + `apps/web/app/root.tsx:38` + `apps/web/public/
manifest.json`. Logo is a single component (`packages/propel/src/icons/brand/plane-logo.tsx`, only
5 importers) plus a handful of static PNGs per app. Theme colors centralized in
`packages/tailwind-config/variables.css`. No existing branding env var (compile-time constants
today, not runtime-configurable). i18n strings: ~106 occurrences of "Plane" across ~27 locale JSON
files per locale — mechanical volume, not architectural risk. **Decision: build as a
`prom_branding` Add-on (Level 2, build-time override files) — no ADR/core patch required.**
`apps/admin`/`apps/space` root.tsx wiring for the same constants was not exhaustively re-verified
and should be spot-checked in Gate 8.

---

## 3. Reuse / Refactor / Rewrite / Retire Matrix

| Item | Source | Status | Reason |
|---|---|---|---|
| LINE Flex Message builders | Plane_Line `views/line/messages.py` | Reference | UX/format idea only; rewrite against new domain model |
| Conversation state design (Redis+TTL+step) | Plane_Line `conversation.py` | Reference | Pattern sound; must live in Hub's own Redis/DB, not shared with Plane core |
| 6-digit linking code concept | Plane_Line | Reference/Rewrite | Matches §7 design intent; implementation must use `secrets` CSPRNG + rate-limiting |
| LINE webhook signature verification | Plane_Line `base.py:40-47` | Reference/Rewrite | Algorithm correct; fail-open bug must be fixed (fail closed) |
| LINE Django models (embedded in core) | Plane_Line `db/models/integration/line.py` | Retire | Deep core embedding violates isolation rules |
| Core notification inline hook | Plane_Line `bgtasks/notification_task.py` | Retire | Must consume Plane's native outbound Webhook instead |
| Plaintext secret storage | Plane_Line | Retire | Violates encryption-at-rest requirement |
| `setup_line.sh` post-save test UX | Plane_Line | Reference | "Test Connection" UX idea for Gate 8 |
| `plane_client.py` (REST wrapper) | Discord middleware | Reference/Refactor | Base for `HttpPromClient` port; auth path discarded |
| Discord command/panel/modal flow | Discord middleware `discord_bot/` | Reference | UX ideas only; code tightly coupled, not portable |
| Webhook debounce/aggregator | Discord middleware `webhook_server/notifications.py` | Reference/Refactor | Directly matches debounce requirement; strongest pattern found |
| Fernet encryption-at-rest pattern | Discord middleware `auth_store.py` | Reference | Pattern reusable, not the password-derived-token code around it |
| Password-login → token flow | Discord middleware `auth_service.py`/`auth_links.py`/`login.py` | Retire | Explicitly forbidden regardless of encryption |
| AI/Intent-parser code | Discord middleware `discord_bot/ai/`, `intent_parser/` | Retire (out of Phase 1) | Must never enter Prom's Phase 1 runtime/build |
| MCP confirm-guard pattern | Discord middleware `mcp_server.py` | Reference (Phase 2 note) | Confirmation-before-write idea; kept as Phase 2 AI extension point |
| Test suite structure | Discord middleware `tests/` | Reference | Contract test shape, not the code itself |
| Single-process, no-queue architecture | Discord middleware `main.py` | Retire | Conflicts with retry/dead-letter/scheduler requirements |

---

## 4. Proposed Architecture

Extension Strategy per §3.1 confirmed feasible: no native Plane plugin system exists, so Levels
1–3 (REST API + outbound webhooks, external Add-ons composed at build, UI mounted via reverse
proxy/nav hook) are the real path, not a fallback. **Core change budget for Phase 1 = 0 patches**
for integration logic (webhook + REST API cover create/query/update/notify). Branding is a Level 2
Add-on. Two Unknowns remain non-blocking for Gate 1 but must close before Gate 8: nav/menu
extension point in `apps/web`/`apps/admin`, and full webhook payload granularity (see §12).

Architecture style: Modular Monolith + Clean/Hexagonal for `prom-integration-hub`, per master
prompt §5, unchanged — no evidence contradicts it.

---

## 5. Decisions Made (Product Owner, 2026-08-18)

| # | Decision | Choice |
|---|---|---|
| 1 | Upstream pin | Pin `plane` at current HEAD `e056bbf9eb` (branch `preview`) |
| 2 | Queue technology | Celery (matches Plane core's own `worker`/`beat-worker`, shared Redis infra) |
| 3 | Phase 1 rebranding scope | Full rebrand from Phase 1, via `prom_branding` Add-on (Level 2) |
| 4 | Delegated-auth investigation sequencing | Investigate before Gate 2 (completed, see §2.4) |
| 5 | API token workspace-scope gap | Accept unscoped (full-account) tokens as a documented Phase 1 tradeoff |

---

## 6. Risks and Unknowns Carried Into Gate 2+

1. Nav/menu extension point in `apps/web`/`apps/admin` not exhaustively verified — needed before Gate 8 UI integration.
2. Webhook payload granularity (does `issue` event payload distinguish assignee-removed, state-change, etc., or only "issue updated") — not verified, needed before Gate 7 event catalog is finalized.
3. `apps/api` CORS/env var list not fully enumerated.
4. AI code boundary leakage in Discord middleware (`resources/work_items/common.py`, `config.py`, `webhook_server/labels.py`) not fully resolved — treat those three files as untrusted for reference purposes until re-verified.
5. Exposed secrets in `Plane_Line` public repo — user's action item, unresolved, tracked outside this codebase.
6. Unscoped API tokens (§2.4, §5.5) — accepted tradeoff, revisit if a stricter isolation requirement emerges.

---

## 7. Gate 2 Scope (this iteration)

Per master prompt §19 Gate 2: project skeleton, configuration, logging, database/migration
foundation, health/readiness, CI quality gates, architecture/import-boundary tests — for
`services/prom-integration-hub/`. No business logic (domain rules, use cases, channel adapters)
is implemented yet; that begins at Gate 3.
