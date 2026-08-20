# Prom Integration Hub — Phase 1 Release Notes

**Status**: Complete. 223 tests passing (197 unit, 26 integration/E2E — all real, all independently
re-verified against live Postgres/Redis). See `docs/architecture/03-phase1-definition-of-done.md`
for the full requirements checklist and `docs/architecture/02-gate9-verification.md` for the final
verification record.

## What Phase 1 ships

A Modular Monolith / Clean-Hexagonal Python service (`services/prom-integration-hub`) connecting
Plane to LINE and Discord, plus two small, documented, reversible patches to Plane's own frontend
and backend (`core_patches/`) exposing an admin UI for it — all built without forking or diverging
from pinned upstream Plane.

- **Account linking**: one-time 6-digit codes (CSPRNG, rate-limited, 10-minute expiry) — a user
  never types a password or API token into LINE or Discord. A delegated Prom API token is minted
  silently, server-side, the moment they click "Connect."
- **Work items via chat**: create (title → project → state → assignee → due date → review →
  confirm), query ("my tasks", assigned/due-today/overdue), update state, comment, reassign, mark
  complete — the identical flow logic drives both LINE (Flex Messages/Quick Replies) and Discord
  (Select Menus/Buttons/a persistent Gateway bot), sharing one channel-agnostic conversation state
  machine.
- **Notifications**: real Plane webhook events (assignee added/removed, state change, comments)
  routed to the right linked identity, debounced (45s window) into one summarized message, with
  retry and dead-lettering on delivery failure. A scheduled sweep catches due-soon/due-today/
  overdue items daily (Asia/Bangkok).
- **Admin UI**: an "Integrations" tab in Plane's own workspace settings (install/configure/test/
  enable-disable/revoke LINE OAs and Discord bots, manage workspace mappings, view an audit log)
  and a "Connected Accounts" tab in profile settings, both relayed through a thin, permission-
  checked Django bridge so Hub's service-token-protected API is never exposed to the browser.

## Architecture decisions worth knowing about

- **Plane is pinned and read-only** at commit `e056bbf9eb`, with exactly 2 documented, minimal,
  reversible patches — because neither Plane's backend nor frontend has any plugin/extension
  mechanism (verified, not assumed). See `core_patches/`.
- **Hub never persists work items** — every work-item read/write is a live call to Plane's REST
  API using a per-user delegated token. Plane's own permission system is always the final word on
  what a user can do.
- **Two historical prototypes** (`Plane_Line`, `Plane-Discord-middleware`) were mined for UX ideas
  and lessons (a fail-open signature bug and a non-cryptographic OTP generator were found in one
  of them and specifically avoided here) but contributed zero lines of code — everything was built
  fresh against a clean architecture.

## Known, documented tradeoffs (not oversights)

- **"My tasks" is client-side filtered with hard caps** (10 projects × 3 pages) — Plane's public
  API has no server-side assignee filter on this edition.
- **API tokens are account-wide, not workspace-scoped** — Plane's token-creation endpoint doesn't
  support scoping; Hub's own `workspace_connections` mapping is the enforced boundary in practice.
- **No in-place credential rotation** — rotating a LINE/Discord credential means creating a new
  installation and re-linking, not editing the existing one.
- **True @-mention parsing isn't implemented** — comment notifications go to the work item's
  current assignees, not specifically whoever was @-mentioned (Plane has no structured mentions
  field to key off).
- **Default-only Asia/Bangkok timezone** for scheduled notifications — not yet per-customer
  configurable.
- **"Move project" is unsupported** — Plane's own API marks `project` read-only on a work item.

## What's explicitly not in Phase 1

No AI/LLM code, no natural-language intent detection, no free-form command execution, no dynamic
workflow builder, no cross-customer shared data plane — per the master prompt's own Phase 1 scope.
Extension points for a future AI phase exist conceptually (see the MCP-server confirm-guard
pattern noted as Reference during Gate 0) but nothing was built toward it.

## Two things flagged for immediate follow-up

1. **No automated "replay a failed event" capability** — raw webhook payloads aren't retained, so
   a dead-lettered inbound event can't currently be safely replayed from scratch (dead-lettered
   *outbound* messages can be). See `docs/operations/runbook.md` §4.
2. **SSRF hardening gap** on Hub's own outbound calls to admin-configured Prom instance URLs (Hub's
   `HttpPromApiClient` doesn't use the DNS-pinning discipline Plane's own webhook delivery and this
   session's Django relay both apply). See `docs/architecture/01-threat-model.md` §8.

Neither blocks a Phase 1 launch — both are documented, bounded, and recommended as the first
post-launch hardening work.
