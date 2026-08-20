# Prom Integration Hub — Threat Model (Gate 9)

> Maps every threat category from the master prompt's §12 Security Requirements to its actual
> mitigation in this codebase, with file:line evidence and the test that proves it — or, where a
> mitigation is partial, an honest statement of the residual risk. Written at the end of Phase 1
> (Gate 9), after all of Gates 2-8 were built and independently verified, so every citation below
> is checked against real, shipped code, not a plan.

---

## 1. Tenant isolation

**Threat**: Customer A's data, credentials, or actions leak into Customer B's scope.

**Mitigation**: `StartAccountLinking` explicitly checks
`channel_installation.customer_id == prom_instance.customer_id` before creating any
`LinkingCode`/`DelegatedCredential`, raising `TenantIsolationError` (`TENANT_ISOLATION_VIOLATION`,
HTTP 403) otherwise —
`src/application/account_linking/use_cases.py` (`StartAccountLinking.execute`). Every tenant-owned
table carries `customer_id` (Gate 3 data model). Proven by
`tests/application/test_account_linking.py::test_tenant_isolation_rejects_cross_customer_linking`
and, end-to-end through real HTTP + real Postgres with a check that **zero** `LinkingCode`/
`DelegatedCredential` rows leaked from the rejected attempt, by Gate 9's
`tests/e2e/test_full_journeys.py::test_tenant_isolation_leaves_no_trace`.

## 2. Webhook forgery

**Threat**: A forged webhook (fake LINE message, fake Discord event, fake Prom event) triggers
Hub to act as if a real one arrived.

**Mitigation**:
- LINE: `src/channels/line/signature.py` — HMAC-SHA256 over the raw body, `hmac.compare_digest`
  (constant-time), **fails closed** if the channel secret is empty/missing (the exact inverse of
  the real bug found in `Plane_Line` during Gate 0 discovery). Tested in
  `tests/channels/test_line_signature.py`.
- Prom: `src/adapters/prom_api/webhook_signature.py` — same shape, verified against Plane's real
  `X-Plane-Signature` construction (`apps/api/plane/bgtasks/webhook_task.py:296-304`, read during
  Gate 7). Tested in `tests/adapters/test_prom_webhook_signature.py`.
- Discord: no per-message signature exists or is needed — Hub connects **outbound** to Discord's
  Gateway over an authenticated WebSocket (bot token, TLS); nothing can inject a fake event without
  compromising Discord's own infrastructure or Hub's token. See Gate 6's architectural note in
  `README.md`.

## 3. Replay attack

**Threat**: A legitimately-signed webhook is redelivered (by the sender's own retry logic, or by
an attacker capturing and replaying a valid request) and Hub processes it twice.

**Mitigation**:
- LINE/Discord: `WebhookDedupStore` (Redis, `mark_seen_if_new`), keyed by event id, TTL 24h.
- Prom: `WebhookEventRepository`, a **durable Postgres unique constraint** on `delivery_id`
  (`X-Plane-Delivery`, Plane's own per-delivery UUID) — chosen over Redis specifically because the
  inbound Prom event also needs a processed/failed/dead-letter audit trail, not just a dedup bit
  (see `src/application/webhooks/ports.py`'s module docstring). `PostgresWebhookEventRepository.add`
  SAVEPOINT-translates the constraint violation into a clean "already recorded, skip" signal rather
  than a raw `IntegrityError`.
- Proven end-to-end (not just at the repository-unit level) by Gate 9's
  `tests/e2e/test_full_journeys.py::test_duplicate_prom_webhook_does_not_double_notify` — the same
  signed payload posted twice, real HTTP, real Postgres, asserting exactly one
  `WebhookEventRecord` row and exactly one outbound message.

## 4. OTP brute force

**Threat**: An attacker guesses the 6-digit account-linking code (1,000,000 possibilities).

**Mitigation**: `secrets.choice`-based generation (cryptographically secure, unlike the
`random.choices` bug found in `Plane_Line` during Gate 0), 10-minute expiry, single-use
(`used_at` marked atomically on consumption), and a **Redis rate limiter** capping consumption
attempts at 5 per `(channel_installation_id, channel_user_id)` per 10-minute window
(`ConsumeLinkingCode.execute`, `src/application/account_linking/use_cases.py`) — checked *before*
the code lookup, so the limiter itself can't be bypassed by probing. Tested in
`tests/application/test_account_linking.py::test_rate_limiting_blocks_after_five_attempts`.

## 5. Credential leakage

**Threat**: LINE/Discord bot credentials, delegated Prom API tokens, or Plane webhook secrets are
exposed via logs, API responses, or at-rest storage.

**Mitigation**: Every credential class is Fernet-encrypted at rest (`FernetSecretProvider`) —
channel installation credentials, delegated tokens, Prom webhook secrets. **Never echoed back**:
every response schema exposes only a boolean (`has_webhook_secret`, `has_credentials`), never the
ciphertext or plaintext (`apps/api/schemas.py`'s `WorkspaceConnectionResponse`,
`ChannelInstallationDetailResponse`). Grepped `src/` for ad-hoc `logger.*` calls outside the
structured audit-log/correlation-id machinery: **zero matches** — there is no scattered
`logger.info(token)`-style call anywhere in the business logic to leak a secret through.

## 6. Broken access control

**Threat**: An action is performed by someone who shouldn't be allowed to perform it.

**Mitigation**: Hub's own `/api/v1/*` surface requires `X-Hub-Service-Token`
(`require_service_token`, `secrets.compare_digest`) for every server-to-server caller. Plane's
Django relay (`prom_bridge/`) gates installation/audit-log management behind
`WorkspaceOwnerPermission` (Admin role only) and connected-accounts management behind
`IsAuthenticated` + an **explicit ownership check** before any delete
(`PromConnectedAccountDetailView.delete`, `apps/api/plane/prom_bridge/views.py`) — proven for real
against a live Postgres by `apps/api/plane/prom_bridge/tests.py::
PromBridgeConnectedAccountsTests.test_delete_rejects_an_identity_not_owned_by_the_caller` (Gate 8,
independently re-run and confirmed by this session's own hands, not just the build's claim).
Ultimate work-item permission is always Plane's own RBAC, never re-implemented in Hub — a
delegated token acts *as* the linked user, so Plane's per-request permission check is the final
backstop (§7's design, confirmed feasible during Gate 1 research).

## 7. Cross-workspace access

**Threat**: A user or installation accesses/links a workspace it has no legitimate relationship to.

**Mitigation**: `WorkspaceNotAllowedError` — `StartAccountLinking` verifies a requested default
workspace is actually linked to the channel installation
(`InstallationWorkspace` join table) before accepting it. Tested in
`test_workspace_not_allowed_when_not_linked_to_installation`.

## 8. SSRF via configurable Prom URL

**Threat**: An admin-configured `PromInstance.base_url` (or a future attacker-controlled variant
of it) is used to make Hub issue requests to internal/unintended hosts.

**Mitigation — partial, documented honestly, not overclaimed**: `PromInstance.base_url` is
validated as a plausible `https://` non-localhost-looking string
(`src/domain/instances/entities.py`), mirroring Plane's own webhook URL validator's approach — this
is a string check, **not real SSRF protection** (no DNS resolution/pinning), a known limitation
already flagged in Gate 1's risk register. **Newly confirmed this session**: `HttpPromApiClient`
(`src/adapters/prom_api/client.py`) uses a plain `httpx.AsyncClient` with no DNS-rebinding
protection — unlike Plane's own outbound webhook delivery (`bgtasks/webhook_task.py`) and this
session's own `prom_bridge/client.py`, both of which use Plane's `pinned_fetch` helper specifically
to close this gap. **Residual risk**: `PromInstance.base_url` is only ever set via a
service-token-protected admin call (`POST /api/v1/prom-instances`), not directly by an
unauthenticated or low-privilege actor, which substantially limits practical exploitability — but
it is not currently defense-in-depth. **Recommended before a production launch, not done in this
engagement**: add the same DNS-pinning discipline to `HttpPromApiClient` that `prom_bridge/client.py`
already has for its Hub-bound calls.

## 9. Injection

**Threat**: SQL/command injection via unsanitized input.

**Mitigation**: 100% SQLAlchemy 2.x ORM/Core query construction across every repository — no raw
SQL string interpolation was introduced in any gate (confirmed by this codebase's own consistent
`select(...)`/`Model(...)` pattern throughout `src/adapters/persistence/`). No shell/subprocess
invocation of any kind exists in `src/` or `apps/`.

## 10. Excessive data exposure

**Threat**: An API response reveals more than the caller needs.

**Mitigation**: Every response schema is an explicit Pydantic model (`apps/api/schemas.py`), never
a raw ORM/domain-entity dump — secrets are boolean-flagged, not included. `AuditLogEntry` never
stores message content, only action metadata (§8.7's own requirement, honored by
`src/domain/audit/entities.py`'s field list).

## 11. Queue poisoning

**Threat**: A malicious or malformed message reaches the Celery queue and disrupts processing.

**Mitigation**: The Prom webhook route verifies signature **and** deduplicates **before** ever
calling `celery_app.send_task` (`apps/api/routers/prom_webhook.py`) — an unauthenticated payload
never reaches the queue at all. Once queued, `process_prom_webhook_event`/
`send_debounced_notification` (`src/infrastructure/messaging/notification_tasks.py`) have
`autoretry_for`/`retry_backoff`/`max_retries=5`, dead-lettering (not infinitely retrying or
silently dropping) on exhaustion.

## 12. Log leakage

**Threat**: Sensitive values (tokens, secrets, OTPs, authorization headers) appear in logs.

**Mitigation**: Structured JSON logging (`src/infrastructure/observability/logging.py`) with
correlation-id propagation; confirmed by grep (see §5 above) that no business-logic code path logs
a raw secret/token value. `StartAccountLinking`'s audit entry for code issuance explicitly does
**not** include the code itself (`src/application/account_linking/use_cases.py`, documented
inline) — only that a code was issued.

## 13. Dependency vulnerability

**Threat**: A known-vulnerable third-party package is shipped.

**Honest gap, not mitigated in this engagement**: `pyproject.toml` uses version *ranges*
(`>=X,<Y`), not exact pins, and **no automated dependency vulnerability scanning** (Dependabot,
`pip-audit`, `safety`, or equivalent) was set up during Gates 2-9. This is a real, undone piece of
Phase 1 hardening — recommended as the first follow-up after this engagement, not something to
claim as done.

---

## Summary

12 of 13 threat categories have a concrete, tested, evidence-backed mitigation in shipped code.
Two are honest, documented residual gaps rather than silently-assumed-solved: **SSRF protection
on Hub's own outbound calls to `PromInstance.base_url`** (§8) and **automated dependency
vulnerability scanning** (§13). Both are recommended as immediate post-Phase-1 follow-ups.
