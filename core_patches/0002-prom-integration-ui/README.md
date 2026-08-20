# Patch 0002: prom-integration-ui — Integrations & Connected Accounts settings UI

## Patch identifier
`0002-prom-integration-ui`

## Upstream commit/version it applies to
`plane` at `e056bbf9eb` (same pinned HEAD as [`0001-prom-bridge`](../0001-prom-bridge/README.md),
branch `preview`, package version `1.4.2`).

## Reason the Add-on/API approach was insufficient

Same finding as `0001-prom-bridge`, re-confirmed for the frontend specifically this session:
`apps/web`'s workspace and profile settings tabs are driven entirely by static, compile-time
TypeScript config objects (`packages/constants/src/settings/workspace.ts`,
`packages/constants/src/settings/profile.ts`) that both gate routing/permissions and drive
sidebar rendering — there is no registry, plugin hook, or runtime extension point to add a tab
from outside the checkout. This is therefore also a **Level 5 (isolated patch set)**: every new
file lives in its own directory (`components/prom-integrations/`,
`components/settings/profile/content/pages/connected-accounts.tsx`), and every edit to an
existing file is a single additive entry into an existing config object/map, never a rewrite of
existing behavior.

## Affected files

**New (all isolated, all under `apps/web/` or `packages/`):**
- `apps/web/core/services/prom-bridge.service.ts` — `PromBridgeService`, thin `APIService`
  subclass hitting `/api/prom-bridge/*` (the relay built in `0001-prom-bridge`), typed request/
  response shapes for every relay endpoint.
- `apps/web/app/(all)/[workspaceSlug]/(settings)/settings/(workspace)/integrations/page.tsx` —
  workspace Integrations settings page (installations list/create/empty-state/audit log),
  admin-gated via `useUserPermissions().allowPermissions([EUserPermissions.ADMIN],
  EUserPermissionsLevel.WORKSPACE)`.
- `apps/web/app/(all)/[workspaceSlug]/(settings)/settings/(workspace)/integrations/header.tsx` —
  `IntegrationsWorkspaceSettingsHeader`, direct pattern copy of the existing
  `WebhooksWorkspaceSettingsHeader`.
- `apps/web/core/components/prom-integrations/create-installation-modal.tsx` — create-only
  installation form (LINE: `channel_access_token` + `channel_secret`; Discord: `bot_token` — exact
  field names verified against `services/prom-integration-hub/src/channels/validator_registry.py`,
  not guessed). Create-only because Hub exposes no update-credentials endpoint (confirmed against
  Stage 1's full route list); rotating credentials means creating a new installation and re-linking
  workspaces to it.
- `apps/web/core/components/prom-integrations/installations-list.tsx` — per-installation actions:
  test-connection, activate/deactivate, revoke (behind `AlertModalCore` confirm), manage-workspaces.
- `apps/web/core/components/prom-integrations/workspace-links-modal.tsx` — shows an installation's
  linked workspaces (`GET installations/{id}/`'s `workspaces` field) with per-row unlink, and a
  "link this workspace" action that calls the `0001-prom-bridge`
  `PromWorkspaceConnectionView` get-or-create endpoint first, then the link endpoint.
- `apps/web/core/components/prom-integrations/audit-log-section.tsx` — simple offset-paginated
  table (occurred_at/action/result_status/error_code); intentionally kept minimal, no filters.
- `apps/web/core/components/settings/profile/content/pages/connected-accounts.tsx` —
  `ConnectedAccountsProfileSettings`: list of the current user's `LinkedIdentity` rows with unlink,
  and a "Connect account" modal.

**Existing files, additive single-purpose edits:**
- `packages/types/src/settings.ts` — added `"integrations"` to `TWorkspaceSettingsTabs`, added
  `"connected-accounts"` to `TProfileSettingsTabs`.
- `packages/constants/src/settings/workspace.ts` — added an `integrations` entry to
  `WORKSPACE_SETTINGS` (reusing the workspace-settings.json's pre-existing but previously unwired
  `workspace_settings.settings.integrations.title`/`.description` i18n keys — see i18n note below),
  added it to `GROUPED_WORKSPACE_SETTINGS[DEVELOPER]` next to `webhooks`.
- `packages/constants/src/settings/profile.ts` — added a `connected-accounts` entry to
  `PROFILE_SETTINGS` and to `GROUPED_PROFILE_SETTINGS[DEVELOPER]` next to `api-tokens`. Uses a new
  `profile.actions.connected-accounts` / `account_settings.connected_accounts.*` key namespace,
  deliberately **not** the pre-existing but also-unwired `account_settings.connections` /
  `profile.actions.connections` keys — those names are ambiguous enough (could plausibly mean
  workspace connections, not channel account links) that reusing them risked a confusing collision
  if upstream ever wires them up for something else. Flagged here as the one i18n-naming judgment
  call in this patch.
- `apps/web/core/components/settings/workspace/sidebar/item-icon.tsx` — added `Plug` (lucide-react)
  to `WORKSPACE_SETTINGS_ICONS.integrations`.
- `apps/web/core/components/settings/profile/sidebar/item-categories.tsx` — added `Link2`
  (lucide-react) to the local `ICONS.["connected-accounts"]` map.
- `apps/web/core/components/settings/profile/content/pages/index.ts` — added a lazy-loaded
  `"connected-accounts"` entry to `PROFILE_SETTINGS_PAGES_MAP`.
- `apps/web/app/routes/core.ts` — added one `route()` entry:
  `:workspaceSlug/settings/integrations` → the new `integrations/page.tsx`.
- `packages/i18n/src/locales/en/settings.json` — added `account_settings.connected_accounts.*`
  block and `profile.actions.connected-accounts`.
- `packages/i18n/src/locales/en/workspace-settings.json` — added a new top-level
  `prom_integrations.*` namespace block (create/list/test/revoke/manage-workspaces/audit-log
  strings); reused the pre-existing `workspace_settings.settings.integrations.title`/`.description`
  keys for the page heading rather than duplicating them.

Only English (`locales/en/`) was updated; other locales fall back to English via i18next's
`fallbackLng`, matching how every other partially-translated string in this codebase already
behaves — no functional gap, just untranslated strings in other languages until someone runs the
project's normal translation pass.

## Scope decision: Connected Accounts "Connect" flow is a two-step picker

The original directive sketched a single "Connect LINE"/"Connect Discord" action. In the real data
model a `LinkedIdentity` is user-scoped (one Plane user, across any workspace they belong to) but
the `ChannelInstallation` it links against is workspace-scoped, and Hub's API (confirmed by reading
`services/prom-integration-hub/apps/api/routers/identities.py` — it only exposes `DELETE
/api/v1/identities/{id}`, nothing else) gives no "list every installation visible to this user
across all their workspaces" endpoint to shortcut this. `ConnectAccountModal` in
`connected-accounts.tsx` therefore asks the user to pick one of their workspaces (from the
already-loaded `useWorkspace().workspaces` store, no new fetch) and then one of that workspace's
installations (`PromBridgeService.listInstallations`), before calling `startLinking`. This is a
deliberate scope decision, not an oversight.

## Verification

The build itself couldn't get a real `pnpm install`/`tsc --noEmit` run going in its sandbox before
running out of session budget (see the manual-review steps below — still real, done first, and
worth keeping as a record of what was checked by hand). That gap was closed immediately afterward
in the same overall session:

1. Manual review of every new/changed file for syntax correctness and that every imported path
   resolves to a real, existing export — traced by hand: `@plane/ui` (`ModalCore`,
   `EModalPosition`, `EModalWidth`, `Input`, `AlertModalCore` — confirmed present via
   `packages/ui/src/index.ts`'s `export * from "./modals"` and the sibling
   `web-hooks/form/input.tsx` import pattern), `@plane/propel/button` (`Button`, variants
   `primary|error-fill|error-outline|secondary|tertiary|ghost|link` confirmed against
   `packages/propel/src/button/helper.tsx`'s `buttonVariants` — no `"danger"` variant exists, an
   initial draft error caught and fixed pre-verification), `@plane/propel/toast`, `@plane/propel/
   empty-state` (`assetKey="webhook"` confirmed a valid asset key), `@plane/i18n`'s `useTranslation`
   (`t(key, params?)` signature confirmed against `packages/i18n/src/hooks/use-translation.ts`).
2. Every new i18n key referenced via `t("...")` in the new components was cross-checked against
   what was actually added to `locales/en/settings.json` / `locales/en/workspace-settings.json` in
   this same patch — no orphaned key reference left calling into a namespace that doesn't define it.
3. `python3 -c "import json; json.load(open(...))"` run against both edited locale JSON files —
   both parse as valid JSON.

**Then, for real, against the actual monorepo** (`node_modules` already present from the build's
own earlier `pnpm install`, ~1.1GB, confirmed intact):

4. `pnpm --filter web run check:types` (`react-router typegen && tsc --noEmit`) → **one real error**
   found on first run: `create-installation-modal.tsx`'s conditional `credentials` object literal
   (LINE's `{channel_access_token, channel_secret}` vs Discord's `{bot_token}`) inferred as a
   discriminated union with `?: undefined` companion keys, not assignable to the
   `Record<string, string>` the service method expects. Fixed with an explicit
   `const credentials: Record<string, string> = …` annotation (one line). Re-ran →
   **exit code 0, full app typecheck clean**, not just the new files.
5. `pnpm --filter web run check:lint` (`oxlint --max-warnings=11957 .`) → **exit code 0**,
   `Found 780 warnings and 0 errors` — confirmed by grep that **zero** of the 780 warnings are in
   any file this patch touches (all pre-existing baseline elsewhere in the app).
6. `pnpm --filter web run check:format` (`oxfmt --check .`) → failed on first run, all 5 new
   component/page files unformatted (the build never ran the formatter). Fixed with
   `pnpm --filter web run fix:format` (auto-formats in place, no logic changes — diffed by eye
   after to confirm). Re-ran `check:format` → **exit code 0, all files correctly formatted**.

This patch has now had the same tier of real verification as `0001-prom-bridge` — real tool runs
against the real monorepo, not just manual review.

## Compatibility test

No automated test exists for this patch (frontend component tests are not a pattern used elsewhere
in `apps/web` for settings pages — confirmed by absence of any `*.test.tsx` file alongside
`webhooks/`, the closest analog). After any future upstream version bump, before re-applying:

1. Confirm `packages/constants/src/settings/{workspace,profile}.ts`'s `WORKSPACE_SETTINGS`/
   `PROFILE_SETTINGS`/`GROUPED_*` shapes are unchanged — every new tab entry depends on the exact
   `{ key, i18n_label, href, access, highlight }` (workspace) / `{ key, i18n_label }` (profile)
   shape.
2. Confirm `apps/web/app/routes/core.ts`'s `route()`/`layout()` DSL is unchanged — the one new
   `route()` call depends on it.
3. Confirm `@plane/ui`, `@plane/propel/button`, `@plane/propel/toast`, `@plane/propel/empty-state`,
   `@plane/i18n` still export the same members used here (listed in Verification above).
4. Re-run `pnpm --filter web run check:types check:lint check:format` (done once already, see
   Verification above — repeat after any upstream bump) plus a manual click-through once a live
   Plane + Hub dev environment is available (browser rendering/click-through has still never been
   done — no running Plane instance was available in this sandbox for that specific check).

## Apply/rollback instructions

**Apply**: this patch is already applied in this working tree (Gate 8 Stage 3). To reapply after a
fresh checkout of `plane` at the pinned commit: copy the new files listed above into place, then
make the additive edits listed above to the 7 existing files.

**Rollback**: delete `apps/web/core/components/prom-integrations/`,
`apps/web/core/components/settings/profile/content/pages/connected-accounts.tsx`, the
`integrations/` settings-page directory, and `apps/web/core/services/prom-bridge.service.ts`;
revert the additive edits to the 7 existing files (each edit is a single added object entry/map
key/`route()` call/i18n block, trivially removable). No database or migration involvement — this
is a pure frontend patch. `0001-prom-bridge` can remain applied independently of this patch (it has
no dependents outside `apps/web`), but this patch depends on `0001-prom-bridge` (calls its relay
endpoints) and should be rolled back first if both are being reverted.

## Owner

Prom platform team (placeholder — assign a real owner before Gate 9 hardening).
