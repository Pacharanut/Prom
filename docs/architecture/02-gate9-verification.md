# Gate 9 — Hardening & Release: Verification Record

> Every result below was independently reproduced by hand after the build's own report — not
> taken on trust. Where a probe was written specifically to re-check a claim, it's described.

---

## Critical finding: writes through the real API were being silently rolled back

**This is the single most important thing found in the entire engagement.** Every prior gate's
verification — including this session's own repeated "independently re-verified with live
Postgres" passes — tested either the **repository layer directly** (`tests/integration/`, its own
session-management fixture) or the **API layer with fakes** (`tests/api/*`, always using
`app.dependency_overrides[get_session]` or overriding individual repositories). Nothing, until
Gate 9's E2E suite, exercised the real FastAPI route → real `get_session()` → real Postgres path
with zero overrides — because that's precisely what "E2E" means and every earlier gate's tests
were, by design, narrower than that.

`src/infrastructure/database/session.py`'s `get_session()` yielded a session inside
`async with session_factory() as session:` and never called `session.commit()`. SQLAlchemy's
`AsyncSession` as a bare context manager only *closes* on exit — closing without an explicit
commit discards the transaction. Every write made through the real, unmocked API — every
`POST`/`PATCH`/`DELETE` a real deployment would ever receive — was returning a correct-looking
`201 Created` and then silently vanishing the instant the request finished. This would have made
the entire Hub non-functional in any real deployment, undetected by 219 passing tests, because
none of them ran the real dependency in the real request lifecycle.

**Fixed** (commit on clean exit, rollback + re-raise on exception — see the file for the full
docstring explaining why):

```python
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

**Independently re-verified by this session, not just trusted from the build's report**: wrote a
standalone probe — `TestClient(create_app())` with **zero** dependency overrides, `POST
/api/v1/customers`, then reread the row via a completely separate raw `asyncpg` connection
(bypassing the app entirely). Before trusting the fix, this is exactly the check that would have
caught the original bug. Real output:

```
HTTP status: 201
HTTP body: {'id': 'd61dfb86-...', 'name': 'Commit-Fix-Probe-Customer', ...}
RESULT: PASS — row found via independent connection: {'id': UUID('d61dfb86-...'), 'name': 'Commit-Fix-Probe-Customer'}
```

**Lesson for future gates/teams**: dependency-override-based API tests and isolated repository
integration tests are both necessary but not sufficient — neither exercises the production
dependency-injection wiring in its true shape. At least one true end-to-end test (real app, real
DB, no overrides on the persistence path) should exist for every write path, and ideally would
have existed from Gate 3 onward rather than Gate 9. Recorded here so it isn't repeated.

---

## E2E test suite (`tests/e2e/test_full_journeys.py`, 4 tests, `@pytest.mark.integration`)

Real HTTP through the actual FastAPI app, real Postgres/Redis, real DI wiring for everything
internal to Hub — only the genuinely-external boundaries (Prom's own API, LINE/Discord message
delivery) are faked, since no live Plane/LINE/Discord instance exists to test against.

1. **Full account-linking journey** — customer → instance → workspace connection → installation
   → workspace link → start-linking → consume OTP → independently re-queried `LinkedIdentity` is
   `active`.
2. **Full work-item journey with idempotent create** — create (with `Idempotency-Key` replay
   proven to skip a second upstream call, via a call-counter on the fake) → state change →
   comment → reassign → complete.
3. **Full notification journey with duplicate-webhook dedup** — HMAC-signed Prom webhook →
   `ProcessPromWebhookEvent` → `FlushOutboundMessage` → fake publisher receives exactly one
   message → the identical signed payload posted again (same `X-Plane-Delivery`) → 200 ack, but
   independently confirmed **zero** additional `WebhookEventRecord` rows and zero additional sends.
4. **Tenant isolation leaves no trace** — cross-customer linking attempt → 403
   `TENANT_ISOLATION_VIOLATION` → independently confirmed zero `LinkingCode`/`DelegatedCredential`
   rows were created for the rejected attempt.

Three real bugs were found and fixed while building this suite (in addition to the critical one
above): a test-seeding helper using a random instead of the real `customer_id` (violated a real FK
constraint that fakes don't enforce); `get_prom_api_client`/`get_redis_client` being unpatchable
bare-function calls inside dependency factories (fixed via `monkeypatch.setattr`, matching the
existing `get_credential_validator` override pattern); and `FakePromApiClient.update_work_item`
never setting `completed_at` on a transition into a completed-group state (a fidelity gap between
the fake and real Prom behavior).

## Confirmation-step design-conformance audit

Read (not newly tested — a design-conformance check against the master prompt's "every significant
write needs confirmation" requirement): `src/application/work_items/conversation.py`. Create work
item goes through an explicit `REVIEW` step (line 182) before `confirm()` executes (line 186).
State-change and reassign are confirmed by the picker selection itself being a single, unambiguous
action naming the exact target (`src/channels/line/handler.py:709,726`). Mark-complete gets an
explicit synthetic `CONFIRM_COMPLETE` step since it has no other input to review
(`conversation.py:256-263`). Comment-adding is the one documented, deliberate exception (no
confirm step — adding a comment doesn't change ownership/state, consistent with §8.4's
"significant-effect writes" scope, documented since Gate 5). No gaps found.

## Migration upgrade/downgrade

All 4 migrations (`0001`-`0004`) already had real, correct `downgrade()` bodies — no fixes needed.
Independently re-run by this session (not just the build's claim):

```
alembic downgrade base
  Running downgrade 0004 -> 0003, notifications and webhooks
  Running downgrade 0003 -> 0002, work items idempotency
  Running downgrade 0002 -> 0001, installation and account-linking schema
  Running downgrade 0001 -> , baseline (no-op)

\dt  ->  only `alembic_version` remains (every other table cleanly dropped, FK-safe order)

alembic upgrade head
  Running upgrade  -> 0001, baseline (no-op)
  Running upgrade 0001 -> 0002, installation and account-linking schema
  Running upgrade 0002 -> 0003, work items idempotency
  Running upgrade 0003 -> 0004, notifications and webhooks

alembic current  ->  0004 (head)
```

## Backup/restore

Seeded realistic data across 12 of 13 tables via the real API (only possible to verify meaningfully
*because* the commit bug above was fixed first — a backup/restore test against silently-empty
tables would have "passed" for the wrong reason). `pg_dump -Fc` from the running container,
restored via `pg_restore` into a fresh, separate throwaway `postgres:16-alpine` container, row
counts and specific field values (customer status, linked identity, webhook event) confirmed to
match exactly. Both containers and the dump file removed afterward.

## Final verification — run twice: once by the build, once independently by this session

| Check | Build's result | This session's independent re-run |
|---|---|---|
| `ruff check .` / `ruff format --check .` | Pass | Pass |
| `mypy src apps` (strict) | Pass, 122 files | Pass, 122 files |
| `pytest -q` (unit) | 197 passed | 197 passed |
| `pytest -m integration` | 26 passed (incl. 4 new E2E) | 26 passed |
| `lint-imports` | 3/3 kept | 3/3 kept |
| `alembic downgrade base && upgrade head` | Clean cycle | Independently reproduced, clean |
| Real-write-persists probe | (implicit in the E2E suite) | Explicit standalone probe, PASS |

**Total: 223 tests, all passing, twice.**
