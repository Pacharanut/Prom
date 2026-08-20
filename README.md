# Prom

Prom is a Project Management product built on top of the open-source [Plane](https://plane.so),
with a central **Prom Integration Hub** connecting it to LINE and Discord. This directory is the
whole workspace for that build — start here if you're new to it.

## Where things are

```
Prom/
├── plane/                          Pinned upstream Plane (read-only by default — see below)
├── services/
│   └── prom-integration-hub/       The actual new product: LINE/Discord integration backend
├── core_patches/                   The only 2 places `plane/` was ever modified — documented,
│                                    minimal, reversible
└── docs/
    ├── architecture/                Discovery, decisions, threat model, DoD checklist
    ├── operations/                  Runbook
    └── release-notes/               Phase 1 release notes
```

## Start here

1. **`docs/architecture/00-gate0-1-discovery-and-plan.md`** — why this is built the way it is:
   what was verified about Plane's real extension points, what the two legacy prototypes were and
   why they were reference-only, and every architecture decision made before any code was written.
2. **`docs/architecture/03-phase1-definition-of-done.md`** — the current state of Phase 1 against
   its original requirements, with evidence for every line.
3. **`docs/architecture/01-threat-model.md`** — every security threat considered and how it's
   mitigated (or, for the 2 items that aren't fully closed, said honestly).
4. **`docs/architecture/02-gate9-verification.md`** — the final hardening pass, including the one
   critical bug it caught (see below).
5. **`docs/operations/runbook.md`** — the 8 operational procedures (rotate a credential, restore
   the database, diagnose a webhook failure, etc.) you'll actually need once this is running.
6. **`docs/release-notes/phase-1.md`** — the short version of all of the above.
7. **`services/prom-integration-hub/README.md`** — setup, run, and test instructions for the
   actual codebase. Start there to get a dev environment running.

## The two historical prototypes are gone

`Plane_Line` and `Plane-Discord-middleware` — a full Plane fork with LINE bolted directly into
Django core, and a separate Discord middleware — were earlier, standalone experiments that were
never part of Prom's runtime or build. They were kept locally only long enough to mine their
Feature Inventory (recorded in the Gate 0/1 doc's Reuse/Refactor/Rewrite/Retire matrix — that
record is what's actually load-bearing, not the source trees themselves) and were deleted once
Phase 1 was complete, per the original plan. **`Plane_Line`'s public GitHub repo (not this local
copy) still has real-looking secrets in its git history** — see the Gate 0 finding in the
architecture doc; that's a separate action item from this local deletion and still needs handling
if it hasn't been already.

## `plane/` is pinned and read-only, with 2 exceptions

`plane/` is pinned at a specific upstream commit (see the architecture doc) and treated as
read-only by default — Prom does not fork or diverge from it. The only two changes ever made to
it are documented, minimal, and reversible: see `core_patches/0001-prom-bridge/` (a new isolated
Django app relaying browser calls to the Hub, ~10 lines touched in 2 existing files) and
`core_patches/0002-prom-integration-ui/` (the Integrations/Connected Accounts settings pages,
similarly minimal additive diffs). Both exist because neither Plane's backend nor its frontend has
any plugin/extension mechanism — verified, not assumed — so a small, well-documented patch was the
only path lower-numbered extension strategies couldn't reach.
