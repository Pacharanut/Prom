# Core Patches

Isolated patches against pinned `plane` upstream, used only when Extension Strategy Levels 1–5
(REST API, Add-on, UI composition, Extension Bridge, isolated patch set) cannot achieve a
requirement — per the project's Extension Strategy Priority.

**Current count: 2.** Gate 1 discovery found no Phase 1 requirement that needed a core patch at
the time — integration logic is covered by Plane's existing REST API and outbound Webhook
feature; branding is achievable as a Level 2 Add-on (see
`docs/architecture/00-gate0-1-discovery-and-plan.md` §4–6). Gate 8 (Prom UI Integration) needed
two, both Level 5 for the same underlying reason (neither Plane's backend nor frontend has any
plugin/extension seam, confirmed across both sessions):

- [`0001-prom-bridge/`](./0001-prom-bridge/README.md) — a browser-safe Django relay to the Prom
  Integration Hub's service-token-protected API, since embedding Hub's shared token in frontend JS
  would be a real security defect.
- [`0002-prom-integration-ui/`](./0002-prom-integration-ui/README.md) — the workspace Integrations
  and profile Connected Accounts settings UI that consumes patch 0001's relay; Plane's settings
  tabs are static compile-time config objects with no seam to add a tab from outside the checkout.

## Format for any future patch

Each patch directory here must include:

- `patch identifier`
- `upstream commit/version` it applies to
- reason the Add-on/API approach was insufficient (with evidence)
- affected files
- automated compatibility test
- apply/rollback instructions
- owner

A patch is added here only with an ADR, per the project's rules.
