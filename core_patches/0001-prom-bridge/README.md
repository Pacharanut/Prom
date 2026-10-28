# Patch 0001: prom_bridge — Django relay for browser-to-Hub admin calls

## Patch identifier
`0001-prom-bridge`

## Upstream commit/version it applies to
`plane` at `e056bbf9eb` (the pinned HEAD from Gate 0/1's architecture decision, branch `preview`,
package version `1.4.2`).

## Reason the Add-on/API approach was insufficient

Gate 8's "Prom UI Integration" scope (Integrations settings page, Connected Accounts, audit
visibility) needs the browser to call the Prom Integration Hub's admin API
(`services/prom-integration-hub`'s `/api/v1/*` routes). Two facts, both verified this session
(not guessed), rule out every lower-numbered Extension Strategy level:

- **No Level 2 (external Add-on, no upstream edit) is achievable.** Neither Plane's backend
  (`apps/api/plane/api/` has zero webhook/plugin/registry mechanism, confirmed Gate 0) nor its
  frontend (`apps/web`/`apps/admin` settings tabs, nav, and API-client wiring are all static,
  compile-time TypeScript config objects — `packages/constants/src/settings/{workspace,profile}.ts`
  — with **zero** matches for `registerRoute|pluginRegistry|extensionPoint|ExtensionRegistry|
  registerPlugin` anywhere in `apps/web`, `apps/admin`, `packages/`, confirmed this session) has
  any seam to compose an Add-on into from outside. This patch, plus the UI patch that will follow
  in Gate 8 Stage 3, are both necessarily **Level 5 (isolated patch set)**.
- **Hub cannot be called directly from the browser.** Hub's entire `/api/v1/*` surface is
  authenticated by one shared `X-Hub-Service-Token` (`services/prom-integration-hub`'s
  `require_service_token`), built for server-to-server calls only. Embedding that token in
  frontend JS would let any logged-in Plane user call any Hub admin endpoint for any customer —
  a real security defect, confirmed by inspecting Hub's actual CORS/auth setup (there is none for
  browser origins) against what a direct browser call would need. The Product Owner confirmed the
  fix during this session: relay through Plane's own session-cookie-authenticated Django backend,
  which alone holds `PROM_HUB_SERVICE_TOKEN` server-side.

## Affected files

**New, isolated (all under one new directory, trivially deletable):**
- `apps/api/plane/prom_bridge/__init__.py`
- `apps/api/plane/prom_bridge/client.py` — `HubClient`, SSRF-safe via the existing
  `plane.utils.url_security.pinned_fetch` helper (the same one `bgtasks/webhook_task.py` already
  uses for outbound webhook delivery — no new HTTP client dependency introduced)
- `apps/api/plane/prom_bridge/views.py` — 13 thin relay views (Gate 8 Stage 3 added
  `PromWorkspaceConnectionView`: `POST workspaces/<slug>/connection/`, get-or-creates the Hub-side
  `WorkspaceConnection` for the current Plane workspace, resolving it itself via
  `Workspace.objects.get(slug=slug)` since `BaseAPIView` only exposes the slug string — needed by
  Stage 3's frontend before it can link an installation to "this" workspace)
- `apps/api/plane/prom_bridge/urls.py`
- `apps/api/plane/prom_bridge/tests.py` — written and, after this doc was first drafted, **executed
  for real** (see Verification below)

**Existing files, minimal diffs (3 files, 3 single-purpose edits):**
- `apps/api/plane/urls.py` — one new line: `path("api/prom-bridge/", include("plane.prom_bridge.urls")),`
  added directly after the existing `path("api/v1/", include("plane.api.urls")),` line.
- `apps/api/plane/settings/common.py` — two edits:
  1. `INSTALLED_APPS` gains `"plane.prom_bridge",` (added for consistency: every other first-party
     `plane.*` app is listed there without exception, confirmed by reading the full list — no
     Django subsystem in this codebase omits a first-party app from `INSTALLED_APPS`, so this
     patch doesn't deviate from that pattern even though `prom_bridge` has no models/migrations).
  2. Four new settings read via `os.environ.get(...)`, same convention as every other
     externally-configured value in this file (e.g. `API_KEY_RATE_LIMIT`, `WEBHOOK_ALLOWED_IPS`):
     `PROM_HUB_BASE_URL`, `PROM_HUB_SERVICE_TOKEN`, `PROM_INSTANCE_ID`, `PROM_CUSTOMER_ID`. All
     default to `None` — a Plane deployment with none of these set is unaffected; every
     `prom_bridge` view detects the missing config and returns `503`, not an unhandled exception.

No `.env.example` file exists anywhere in this `plane/` checkout to update (confirmed by search —
Plane's own `CONTRIBUTING.md` generates `apps/api/.env` via `./setup.sh`, not a committed
template). The four new vars are documented inline as comments in `settings/common.py` instead.

### Phase 2 addition: AI Assistant settings relay (2026-10-26)

Same pattern, same files, no new dependency:
- `views.py` — `PromAiSettingsView` (GET/PUT) and `PromAiSettingsTestView` (POST), both
  `WorkspaceOwnerPermission`, relaying to Hub's `/api/v1/customers/{PROM_CUSTOMER_ID}/ai-settings`
  (+ `/test`). Only the fields Hub accepts are forwarded (`_pick`), so a browser can't smuggle a
  different `customer_id`. Hub never returns the API key, so nothing secret flows back.
- `urls.py` — `workspaces/<slug>/ai-settings/` and `workspaces/<slug>/ai-settings/test/`.
- `tests.py` — `PromBridgeAiSettingsTests` (Member rejected, field filtering, test relay).

Phase 2B (same day) added `PromConnectedAccountNotificationPreferencesView` (GET/PUT
`connected-accounts/<id>/notification-preferences/`, IsAuthenticated). Like unlink, it first
confirms the identity belongs to the signed-in user (`_reject_unless_owned`, factored out of the
delete view) and forwards only the `preferences` object. 3 more tests; suite now 18/18.

Phase 2C added `PromClassificationView` (GET/PUT `workspaces/<slug>/classification/`,
WorkspaceOwnerPermission): it get-or-creates the Hub workspace connection for the slug (same call
as `PromWorkspaceConnectionView`, factored into `_workspace_connection_id`), then relays only
`is_enabled`/`use_ai_fallback`/`rules`. 2 more tests; suite 20/20.

Phase 2E: `PromAuditLogView` now forwards the searchable-history filters (`channel_type`,
`action`, `target_id`, `result_status`, `occurred_after/before`) from an allow-list. Suite 21/21.

AI settings are per Prom customer (one per Plane instance), not per workspace — the same scope
and the same Admin gate as integrations.

Verified 2026-10-26: `python manage.py test plane.prom_bridge` inside `plane-api-1` — 15/15 pass
(the first time this suite was actually executed; see the module docstring), and a live
`HubClient` call from the container reached Hub and, through it, the NT AI Hub gateway.

## Verification

Gate 8 Stage 2's build could not get a live Django environment running in its sandbox
(`psycopg[c]` failed to compile — no `pg_config` available) and shipped with only
`python3 -m py_compile` on the new files plus hand-written, unexecuted tests. That gap was closed
immediately afterward in the same session:

1. `brew install libpq` (provides `pg_config`), fresh venv, `pip install -r apps/api/requirements/base.txt`
   — installed cleanly.
2. `DJANGO_SETTINGS_MODULE=plane.settings.production REDIS_URL=... SECRET_KEY=... python3
   manage.py check` → **`System check identified no issues (0 silenced)`** — confirms
   `plane.prom_bridge` registers correctly, `urls.py`'s new `include()` resolves, and every new
   file imports cleanly.
3. A throwaway `postgres:15-alpine` container (`docker run`, port 55433, discarded after), full
   Plane migration history applied automatically by the test runner (122 `db.*` migrations +
   `django_celery_beat` + `license`, all `OK`), then `python3 manage.py test plane.prom_bridge
   --verbosity=2` → **`Ran 7 tests in 0.134s — OK`**, all real, against a real Postgres:
   `test_unconfigured_hub_returns_503_not_a_crash`,
   `test_delete_forwards_when_identity_is_owned_by_the_caller`,
   `test_delete_rejects_an_identity_not_owned_by_the_caller` (the security-critical
   ownership-enumeration check — confirmed rejecting with a real 404, not just asserted in code),
   `test_start_linking_mints_a_token_and_relays_it`, `test_hub_error_body_and_status_pass_through`,
   `test_admin_role_reaches_the_relay`, `test_member_role_is_rejected_from_installation_list`
   (confirmed a real 403 for a non-admin workspace member).
4. Gate 8 Stage 3 re-ran this same recipe (fresh throwaway container, port 55434) after adding
   `PromWorkspaceConnectionView` + its 2 tests → **`Ran 9 tests in 0.146s — OK`**. Confirmed for
   real (not assumed) that an unknown workspace slug gets **403**, not 404 — `WorkspaceOwnerPermission`
   denies before the view's own `Workspace.DoesNotExist` branch is ever reached, since its
   `WorkspaceMember` filter is simply empty for a nonexistent workspace. The view's 404 branch is
   consequently unreachable for any authenticated caller in practice; left in as harmless defensive
   code, documented here rather than silently removed.
4. Container and temp venv removed afterward; `git status --short` in `plane/` confirmed
   unchanged from before this verification pass (still exactly the 2 modified files + 1 new
   directory listed above, nothing else touched).

## Compatibility test

Automated: `apps/api/plane/prom_bridge/tests.py` (see Verification — written, not yet run in a
live environment). After any future upstream version bump, before re-applying this patch:

1. Confirm `apps/api/plane/urls.py`'s `urlpatterns` list still has the same flat
   `path(prefix, include(...))` structure — the one new line depends on that shape.
2. Confirm `WorkSpaceAdminPermission`/`WorkspaceOwnerPermission`
   (`apps/api/plane/app/permissions/workspace.py`) and `BaseAPIView`
   (`apps/api/plane/app/views/base.py`, `authentication_classes = [BaseSessionAuthentication]`,
   `workspace_slug` property reading `self.kwargs.get("slug")`) still exist with the same
   signatures — every `prom_bridge` view depends on this exact inheritance shape.
3. Confirm `APIToken.objects.create(label=, description=, user=, user_type=, expired_at=)`
   (`apps/api/plane/db/models/api.py`) and `APITokenSerializer` still expose a `token` field in
   the create-response payload — `PromConnectedAccountsStartLinkingView` depends on this.
4. Re-run `apps/api/plane/prom_bridge/tests.py` in a real dev environment.

## Apply/rollback instructions

**Apply**: this patch is already applied in this working tree (Gate 8 Stage 2). To reapply after
a fresh checkout of `plane` at the pinned commit: copy `apps/api/plane/prom_bridge/` in whole,
then make the 3 single-line/single-block edits listed above to `urls.py` and `settings/common.py`.

**Rollback**: delete `apps/api/plane/prom_bridge/` entirely; revert the 1 line in `urls.py`; revert
the `"plane.prom_bridge",` line and the 4-setting block in `settings/common.py`. No migrations
exist for this app (no models), so no database rollback is needed. No other file in `plane/`
imports anything from `prom_bridge`, so this is a clean, total removal.

## Owner

Prom platform team (placeholder — assign a real owner before Gate 9 hardening).
