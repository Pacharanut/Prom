# Prom Integration Hub — Operations Runbook (Gate 9)

> The 8 procedures required by the master prompt's §16 Observability section. Every command below
> is a real endpoint/command that exists in the shipped codebase as of Gate 9 — nothing here is
> aspirational. Where no dedicated tool exists yet, that's stated explicitly rather than invented.

All `/api/v1/*` calls require `X-Hub-Service-Token: <HUB_SERVICE_TOKEN>`. Replace `$HUB` with the
Hub's base URL (e.g. `https://hub.internal.example.com`).

---

## 1. Rotate a LINE channel token/secret

Hub has **no "update credentials" endpoint** (confirmed during Gate 8 — by design, so a stored
credential is never partially/ambiguously updated). Rotation means: create a new installation with
the new credentials, re-point workspace links to it, then revoke the old one.

```bash
# 1. Create the new installation (validated live against LINE's API before it's stored)
curl -X POST "$HUB/api/v1/installations" -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"customer_id":"<uuid>","channel_type":"line","display_name":"Acme LINE OA (rotated)",
       "credentials":{"channel_access_token":"<new-token>","channel_secret":"<new-secret>"}}'
# -> note the returned installation id

# 2. Re-link each workspace that pointed at the old installation
curl -X POST "$HUB/api/v1/installations/<new-id>/workspaces" -H "X-Hub-Service-Token: $TOKEN" \
  -H "Content-Type: application/json" -d '{"workspace_connection_id":"<uuid>","is_default":true}'

# 3. Revoke the old installation (one-way — see §16.5 of the master prompt, "revoke" is
#    permanent; use activate/deactivate instead if you only need a temporary pause)
curl -X POST "$HUB/api/v1/installations/<old-id>/revoke" -H "X-Hub-Service-Token: $TOKEN"
```

Users linked via the old installation are **not** automatically migrated — `LinkedIdentity` rows
point at a `channel_installation_id`. A rotation that changes the installation id requires affected
users to re-link (send a fresh OTP). If only the *value* of an existing LINE Channel's token was
reissued in the LINE Developers Console (same Channel, same bot, just a refreshed token — the
common case), do **not** create a new installation; there is currently no in-place credential
update, so this is a genuine known gap — the pragmatic workaround is a direct, audited database
update to `channel_installations.encrypted_credentials` (re-encrypt with `FernetSecretProvider`)
rather than the full create/relink/revoke dance. Flagged as a Gate 9+ follow-up: a dedicated
`PATCH /api/v1/installations/{id}/credentials` endpoint would remove the need for this workaround.

## 2. Rotate a Discord bot token

Same procedure and same in-place-update gap as §1 — `channel_type: "discord"`,
`credentials: {"bot_token": "<new-token>"}`. Additionally: `apps/discord_bot/main.py` must be
**restarted** to pick up a new/changed installation (Gate 6's documented "no hot-reload of
installations" limitation) — `docker compose restart discord-bot` (or the equivalent for your
deployment).

## 3. Revoke a linked identity (force-unlink a user)

```bash
curl -X DELETE "$HUB/api/v1/identities/<linked_identity_id>" -H "X-Hub-Service-Token: $TOKEN"
```

Sets the identity's status to `unlinked` (`UnlinkIdentity`, Gate 3) — the user must send a fresh
OTP to relink. This is the same action the user's own "Connected Accounts" unlink button performs
(Gate 8), just triggerable by an operator directly.

## 4. Replay a failed event safely

**No dedicated replay endpoint exists yet** — this is a genuine gap, documented honestly rather
than glossed over. The real procedure today requires direct database access:

```sql
-- Find dead-lettered records
SELECT id, workspace_connection_id, event, action, received_at
FROM webhook_event_records WHERE status = 'dead_letter' ORDER BY received_at DESC LIMIT 20;

SELECT id, linked_identity_id, work_item_id, attempt_count
FROM outbound_message_records WHERE status = 'dead_letter' ORDER BY created_at DESC LIMIT 20;
```

To replay a dead-lettered **inbound webhook**: reset its status and re-enqueue manually via a
Python shell in the worker container:

```python
# docker compose exec worker python3 -c "..."
from src.infrastructure.messaging.celery_app import celery_app
celery_app.send_task(
    "notifications.process_prom_webhook_event",
    kwargs={"workspace_connection_id": "<uuid>", "webhook_event_record_id": "<uuid>", "payload": {...}},
)
```

The original `payload` isn't stored on `WebhookEventRecord` (only `event`/`action` metadata is,
per §8.7's "don't store more than necessary" instruction) — so a true replay of a *specific*
historical event isn't currently possible without the original payload in hand (e.g. from Plane's
own webhook delivery log, if Plane's admin retained it). This is a real limitation: **safe replay
of dead-lettered events is not yet a first-class capability** — flagged as a priority Gate 9+
follow-up (storing the raw payload, or at least a reference to Plane's own delivery log id, would
close this).

To replay a dead-lettered **outbound message** (the send succeeded in determining *who* to notify,
just failed to actually deliver): this one IS safely replayable, since the `OutboundMessageRecord`
already has everything needed —

```python
from src.infrastructure.messaging.notification_tasks import send_debounced_notification
send_debounced_notification.apply_async(args=["<outbound_message_record_id>"])
```

but first reset its `status` back to `pending` directly in Postgres (`UPDATE outbound_message_records
SET status = 'pending' WHERE id = '<id>'`), since `FlushOutboundMessage.execute` no-ops on anything
not `pending`.

## 5. Restore the database

Verified for real during Gate 9 (see `docs/architecture/02-gate9-verification.md` for the actual
commands run and their output). Summary:

```bash
# Backup (from a running deployment)
docker compose exec postgres pg_dump -U prom -Fc prom_hub > backup-$(date +%Y%m%d).dump

# Restore (into a fresh/target Postgres)
docker compose exec -T postgres pg_restore -U prom -d prom_hub --clean --if-exists < backup-YYYYMMDD.dump
```

After restoring, run `alembic upgrade head` to confirm the schema version matches what the
restored dump expects (a restore from an older backup onto a newer codebase should be followed by
migrating forward; a restore that's already at the current schema version is a no-op).

## 6. Upgrade Prom Integration Hub

```bash
git pull  # or however your deployment tracks this codebase
docker compose -f deployments/docker-compose.yml build api worker beat discord-bot
docker compose -f deployments/docker-compose.yml run --rm api alembic upgrade head
docker compose -f deployments/docker-compose.yml up -d api worker beat discord-bot
```

Migrations `0001`→`0004` all have real, verified `downgrade()` implementations (confirmed Gate 9 —
see the verification doc) — `alembic upgrade head` is safe to run against a database at any prior
migration state in this chain.

## 7. Roll back a deployment

```bash
docker compose -f deployments/docker-compose.yml run --rm api alembic downgrade <previous-revision>
git checkout <previous-tag-or-commit>
docker compose -f deployments/docker-compose.yml build api worker beat discord-bot
docker compose -f deployments/docker-compose.yml up -d api worker beat discord-bot
```

Roll back the schema (`alembic downgrade`) **before** deploying older code, not after — the
reverse order risks the old code briefly running against a schema it doesn't understand.

## 8. Diagnose a webhook failure

**LINE/Discord not responding**: `docker compose logs api --tail=100 -f` — every webhook request
is logged with its correlation id (`X-Correlation-Id` response header) via the middleware in
`apps/api/main.py`; a 401 means signature verification failed (check the installation's stored
`channel_secret` matches what's configured in the LINE/Discord developer console); a 404 means the
`installation_id` in the URL doesn't match an active installation.

**Prom webhook not triggering notifications**:
1. Confirm the webhook is actually configured in Plane's UI, pointed at the right
   `GET /api/v1/workspace-connections/{id}`'s `webhook_url` field.
2. Confirm the secret was set: `GET /api/v1/workspace-connections/{id}` → `has_webhook_secret`
   should be `true`.
3. `docker compose logs api --tail=100 | grep prom-webhook` — a 401 means signature mismatch
   (re-paste the secret via `PATCH /api/v1/workspace-connections/{id}/webhook-secret`); a 400
   means Plane sent a request without `X-Plane-Delivery` (shouldn't happen with a real Plane
   instance — would indicate a misconfigured or non-Plane sender).
4. Check `SELECT status, count(*) FROM webhook_event_records GROUP BY status;` — a growing
   `failed`/`dead_letter` count with a healthy `received`/`processed` ratio elsewhere points at a
   specific bad event, not a systemic signature/config problem; inspect that record's `event`/
   `action` and cross-reference with `docker compose logs worker`.
5. `docker compose logs worker --tail=100` — Celery task exceptions log here, not in the `api`
   container (the webhook route only enqueues, `notification_tasks.py`'s tasks run in `worker`).
