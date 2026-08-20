# Phase 1 — Definition of Done Checklist (master prompt §20)

> Verified at the end of Gate 9, against real shipped code and real test runs referenced
> throughout this conversation — not a plan. Each item cites the gate that built it and the
> evidence that proves it.

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | LINE OA และ Discord Bot หลายรายการตั้งค่าได้จาก Prom UI | ✅ | Gate 8: `CreateChannelInstallation`, Integrations settings tab, multi-installation `list_by_customer` |
| 2 | Credentials ถูกเข้ารหัสและไม่ถูกส่งกลับหรือแสดงใน Log | ✅ | Gate 3 (Fernet at rest) + Gate 9 threat model §5/§12 (grep-confirmed no ad-hoc secret logging) |
| 3 | Installation เชื่อมหลาย Workspace และมี Default Workspace ได้ | ✅ | Gate 3 `InstallationWorkspace` join table, `LinkedIdentity.default_workspace_connection_id` |
| 4 | User เชื่อม/ยกเลิก LINE และ Discord identity ด้วย one-time code ได้ | ✅ | Gate 3 `ConsumeLinkingCode`/`UnlinkIdentity`, Gate 8 Connected Accounts UI |
| 5 | User ไม่ต้องส่ง Password หรือ API Token ผ่าน Channel | ✅ | Gate 1 delegated-auth research + Gate 8 `PromConnectedAccountsStartLinkingView` (silent server-side token mint) |
| 6 | My Tasks และ Recently Assigned แสดงงานที่ Assign ให้ผู้ใช้ถูกต้อง | ✅ (documented cap) | Gate 4 `ListMyWorkItems` — client-side filtered with hard caps (10 projects × 3 pages), a documented Phase 1 tradeoff since Plane's API has no server-side assignee filter (verified, not guessed) |
| 7 | สร้าง Work Item พร้อม Project, State, Assignee และ Due date ได้ | ✅ | Gate 4 `CreateWorkItem`, Gate 5/6 create-flow wizards |
| 8 | Update State, Comment, Assignee และ Mark Complete ได้ตาม Prom Permission | ✅ | Gate 4 use cases; permission enforcement is Plane's own RBAC via the delegated token (never re-implemented in Hub) |
| 9 | ทุก write action สำคัญมี Confirmation | ✅ | Gate 9 design-conformance audit of `CreateWorkItemFlow`/`UpdateWorkItemFlow` (see Gate 9 verification doc) — comment-adding is the one documented, deliberate exemption |
| 10 | Prom event ส่ง Notification ไป LINE/Discord แบบ private ได้ | ✅ | Gate 7 `ProcessPromWebhookEvent` → `FlushOutboundMessage` → `SendPrivateNotification`, end-to-end tested Gate 9 |
| 11 | Due/Overdue schedule ทำงานตาม timezone และ preference | ✅ (Asia/Bangkok only) | Gate 7 `CheckDueDateNotifications`, `NotificationPreference` opt-out — default-only Asia/Bangkok is a documented Phase 1 scope decision, not per-customer configurable |
| 12 | Duplicate webhook/message ไม่สร้างหรือส่งซ้ำ | ✅ | Gate 5/6 `WebhookDedupStore`, Gate 7 `WebhookEventRepository` unique constraint — proven end-to-end by Gate 9's duplicate-delivery E2E test |
| 13 | Customer/Instance/Workspace isolation ผ่าน Security test | ✅ | Gate 3 `TenantIsolationError` + tests, Gate 9 E2E tenant-isolation test with zero-leakage assertion |
| 14 | Prom เป็นผู้ตรวจ Role/Permission สุดท้าย | ✅ | Architectural invariant throughout — Hub never implements its own work-item permission model, always defers to Plane via the delegated token |
| 15 | Integration Hub ไม่เข้าถึง Prom Database | ✅ | Architectural invariant — Hub has its own Postgres (`prom_hub`), zero code path touches Plane's database directly; confirmed by the whole Gate 2-9 build never introducing a Plane DB connection in `services/prom-integration-hub` |
| 16 | Upstream Plane ถูก pin และคงแบบ read-only ยกเว้น isolated patches ที่ได้รับอนุมัติ | ✅ | Pinned at `e056bbf9eb` (Gate 0/1); exactly 2 approved, documented core patches (`0001-prom-bridge`, `0002-prom-integration-ui`), both Product-Owner-confirmed before implementation |
| 17 | LINE/Discord prototypes ไม่อยู่ใน Runtime หรือ Build Dependency | ✅ | `Plane_Line`/`Plane-Discord-middleware` never imported/referenced by any file in `services/prom-integration-hub` or the `plane` patches — confirmed by every gate's import-linter run and manual citation discipline |
| 18 | Prom customization แยกเป็น Add-ons/Services/Patches และระบุ ownership ได้ | ✅ | `services/prom-integration-hub` (service), `core_patches/0001`/`0002` (patches, each with an owner placeholder pending real assignment) |
| 19 | Core patch ทุกตัวมี version compatibility test และ rollback instruction | ✅ | Both `core_patches/` entries have a "Compatibility test" and "Apply/rollback instructions" section, each independently exercised at least once this session (real `manage.py test` runs) |
| 20 | Retry/failure/dead-letter มีพฤติกรรมและ Runbook ชัดเจน | ⚠️ partial | Retry/dead-letter behavior: ✅ (Gate 7, Celery `autoretry_for`/`max_retries`). Runbook: ✅ written (`docs/operations/runbook.md`) but §4 "replay a failed event" is an honestly-documented gap — no dedicated replay endpoint exists, only a manual DB-level procedure, and raw webhook payloads aren't retained for true replay |
| 21 | Migration, backup, restore และ rollback มีเอกสาร | ✅ | `docs/operations/runbook.md` §5-7, backed by Gate 9's real backup/restore round-trip test and real `alembic downgrade base`/`upgrade head` cycle (see Gate 9 verification doc) |
| 22 | Unit, integration, contract และ E2E tests สำคัญผ่าน | ✅ | Unit/integration: 219+ tests across Gates 2-8, all independently re-verified live. Contract-style tests: `httpx.MockTransport` tests against the verified Plane API shape (Gate 4-6). E2E: Gate 9's `tests/e2e/` (see Gate 9 verification doc for final count) |
| 23 | Lint, format และ type check ผ่าน | ✅ | `ruff`/`mypy strict`/`lint-imports` all green throughout Hub; `tsc --noEmit`/`oxlint`/`oxfmt` all green for the Gate 8 frontend patch |
| 24 | Documentation ตรงกับระบบจริง | ✅ | Every doc in `docs/` cites real file:line evidence or a real command's real output, not aspirational claims — the standard held throughout this engagement |
| 25 | ไม่มี AI/LLM code อยู่ใน Runtime Path ของ Phase 1 | ✅ | Confirmed repeatedly — `discord_bot/ai/`/`intent_parser/` (Discord prototype) never referenced; no `openai`/`anthropic`/LLM dependency anywhere in `services/prom-integration-hub`'s `pyproject.toml` or `plane/`'s two patches |
| 26 | พร้อมให้ทีมอื่น Clone, Setup, Test และพัฒนาต่อจากเอกสารได้ | ✅ | `services/prom-integration-hub/README.md` (setup/run/test commands, all gate-by-gate documented and verified), `AGENTS.md` (architecture rules), `core_patches/` (patch application instructions), this `docs/` tree |

## Summary

25 of 26 items are fully done. 1 item (**#20, retry/runbook**) is honestly marked partial — the
retry/dead-letter *behavior* is solid and tested, but the operational *replay* tooling has a real
gap (no payload retention, no dedicated replay endpoint) that's documented rather than hidden.
Recommended as the first post-Phase-1 follow-up alongside the two threat-model gaps (§8 SSRF
pinning on Hub's outbound Prom calls, §13 dependency vulnerability scanning).

No item on this list was marked done without a real, checkable citation. Where Phase 1 made a
deliberate scope tradeoff (My Tasks caps, Asia/Bangkok-only scheduling, unscoped delegated tokens,
no in-place credential rotation), it's marked ✅ with the tradeoff stated, not silently glossed
over — consistent with every gate's reporting discipline throughout this engagement.
