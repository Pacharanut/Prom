# Running Prom locally for testing

This is the actual, verified procedure for bringing up both Plane and the Prom Integration Hub
side by side on one machine and connecting them — not a plan, a record of what was run and
confirmed working. Every port and workaround below was hit for real; see "Known gotchas" for why
each one exists.

## Current state (as of last setup)

| Service | URL | Notes |
|---|---|---|
| Plane web app | http://localhost:3010 | not :3000 — see gotcha #2 |
| Plane admin (god-mode) | http://localhost:3001/god-mode/ | register instance admin here first |
| Plane Django API | http://localhost:8000 | |
| Plane Postgres | localhost:5433 | not :5432 — see gotcha #3 |
| Hub API | http://localhost:8001 | not :8000 — see gotcha #4 |
| Hub Postgres | localhost:55432 | |
| Hub Redis | localhost:6380 | not :6379 — see gotcha #4 |
| Local TLS proxy | https://plane.127.0.0.1.nip.io:8443 → Plane's API | see gotcha #5 |

Bootstrapped in the Hub for this test setup:
- Customer `09de599d-9f35-42b3-b7a7-dfffb71ea810` ("Local test customer")
- PromInstance `e6892005-3de2-41ff-b433-51e009e8eb3b` (base_url = the TLS proxy URL above)

`plane/apps/api/.env` has `PROM_HUB_BASE_URL`, `PROM_HUB_SERVICE_TOKEN`, `PROM_INSTANCE_ID`,
`PROM_CUSTOMER_ID` set to match. Connectivity Plane-container → Hub and Hub-container → Plane (via
the TLS proxy) were both tested directly and confirmed working.

## What's still manual (deliberately — see root instructions on account creation)

1. Open http://localhost:3001/god-mode/ and register yourself as instance admin.
2. Log in at http://localhost:3010 with the same credentials, create a workspace.
3. Workspace **Settings → Integrations** should now load (not show "not configured") — create a
   LINE or Discord installation to exercise the real Hub-backed flow.
4. Profile **Settings → Connected Accounts** — exercise the linking flow.

## Bringing it up from scratch

### 1. Plane

```bash
cd plane
./setup.sh   # generates apps/api/.env, apps/web/.env, etc. — skip if they already exist

# Port 5433, not 5432 (see gotcha #3) — this repo (not tracked, see .git/info/exclude)
docker compose -f docker-compose-local.yml -f docker-compose-local.override.yml up -d --build

# Migrations + gotcha #1's fix (needed after every image rebuild/container recreate):
docker exec plane-api-1 pip install django-debug-toolbar==6.0.0
docker exec plane-api-1 python manage.py migrate
docker restart plane-api-1   # picks up debug_toolbar + confirms migrations are seen

# Frontend (port 3010, not 3000 — see gotcha #2):
pnpm --filter admin dev &                        # :3001
pnpm --filter web exec react-router dev --port 3010 &
```

If `docker-compose-local.override.yml` doesn't exist (e.g. fresh clone), recreate it:
```yaml
# plane/docker-compose-local.override.yml — untracked, local-only (see gotcha #3)
services:
  plane-db:
    ports: !override
      - "5433:5432"
```

### 2. TLS proxy (needed before creating a PromInstance — gotcha #5)

```bash
cd local-tls   # cert.pem/key.pem/nginx.conf — gitignored, regenerate if missing:
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 \
  -subj "/CN=plane.127.0.0.1.nip.io" -addext "subjectAltName=DNS:plane.127.0.0.1.nip.io"

docker run -d --name plane-tls-proxy -p 8443:8443 --restart unless-stopped \
  -v "$PWD/cert.pem:/certs/cert.pem:ro" \
  -v "$PWD/key.pem:/certs/key.pem:ro" \
  -v "$PWD/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine
```

### 3. Prom Integration Hub

```bash
cd services/prom-integration-hub
cp .env.example .env
# fill in SECRET_ENCRYPTION_KEY and HUB_SERVICE_TOKEN (commands are in the .env.example comments)
# PROM_API_BASE_URL should be http://host.docker.internal:8000

docker compose -f deployments/docker-compose.yml up -d --build
docker exec deployments-api-1 alembic upgrade head
docker stop deployments-discord-bot-1   # until a real Discord installation exists — gotcha #6
```

### 4. Trust the TLS proxy's cert inside Hub's containers (gotcha #5, redo after every rebuild)

```bash
for c in deployments-api-1 deployments-worker-1 deployments-beat-1; do
  docker cp local-tls/cert.pem "$c:/tmp/local-test-ca.pem"
  docker exec "$c" bash -c 'cat /tmp/local-test-ca.pem >> $(python3 -c "import certifi; print(certifi.where())")'
done
```

### 5. Bootstrap a Customer + PromInstance (one-time per Hub database)

```bash
TOKEN=<HUB_SERVICE_TOKEN from services/prom-integration-hub/.env>

CUSTOMER_ID=$(curl -s -X POST http://localhost:8001/api/v1/customers \
  -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Local test customer"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

INSTANCE_ID=$(curl -s -X POST http://localhost:8001/api/v1/prom-instances \
  -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"customer_id\": \"$CUSTOMER_ID\", \"name\": \"Local Plane\", \"base_url\": \"https://plane.127.0.0.1.nip.io:8443\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

### 6. Wire Plane to the Hub

Append to `plane/apps/api/.env`:
```
PROM_HUB_BASE_URL="http://host.docker.internal:8001"
PROM_HUB_SERVICE_TOKEN="<same HUB_SERVICE_TOKEN>"
PROM_INSTANCE_ID="<from step 5>"
PROM_CUSTOMER_ID="<from step 5>"
```
Then recreate (not just restart — env_file changes need a recreate) and reinstall debug_toolbar again:
```bash
docker compose -f docker-compose-local.yml -f docker-compose-local.override.yml up -d api
docker exec plane-api-1 pip install django-debug-toolbar==6.0.0
docker restart plane-api-1
```

## Known gotchas (all found and fixed for real, not anticipated)

1. **`plane-api`'s cached Docker image is missing `django-debug-toolbar`.** Plane's own
   `docker-entrypoint-api-local.sh` and the `migrator` service both pass
   `--settings=plane.settings.local`, which unconditionally requires `debug_toolbar`
   (`apps/api/plane/settings/local.py`). `Dockerfile.dev` does install
   `requirements/local.txt` (which has it), but a full `docker compose build` currently fails
   independently — `apk add` in `Dockerfile.dev` can't resolve `linux-headers`, `make`,
   `nodejs-current`, `postgresql-dev`, `xmlsec` against the current Alpine package index (base
   image drift, unrelated to Prom). Rather than edit the pinned `Dockerfile.dev`, the fix is to
   `pip install django-debug-toolbar==6.0.0` directly into the running container after it starts.
   **This does not persist** — redo it after every `docker compose up --build` / container
   recreate (steps above already include it where needed).

2. **Plane's own default port 3000 is taken.** There's a separate, already-running production-like
   `plane-app` Docker Compose stack on this machine (not part of this project — do not touch it)
   whose proxy publishes host port 3000. The dev `web` app was started on **3010** instead
   (`pnpm --filter web exec react-router dev --port 3010`, bypassing the hardcoded
   `--port 3000` in `apps/web/package.json`'s `dev` script rather than editing that tracked file).
   `admin` (3001), `space` (3002), `live` (3100) don't conflict and were left alone (`space`/`live`
   weren't started at all — not needed for testing the integration).
   `apps/{web,admin,space,live}/.env`'s `*WEB_BASE_URL` and `apps/api/.env`'s
   `APP_BASE_URL`/`CORS_ALLOWED_ORIGINS` were updated to match.

3. **Port 5432 is taken by a native macOS Postgres**, unrelated to any Docker stack. Fixed with an
   **untracked** `plane/docker-compose-local.override.yml` remapping `plane-db` to host `5433`
   (needs the `!override` YAML merge tag — Compose otherwise *appends* to the base file's `ports:`
   list instead of replacing it, so both 5432 and 5433 get published and 5432 still fails).

4. **Hub's own defaults (API :8000, Redis :6379) collide with Plane's docker-compose-local.yml.**
   Fixed in `services/prom-integration-hub/deployments/docker-compose.yml` (committed, not a
   workaround) — API now publishes on **8001**, Redis on **6380**, and api/worker/beat/discord-bot
   get explicit `environment:` overrides so they reach Postgres/Redis by service name instead of
   the `.env` file's host-oriented `localhost` values (which only make sense for natively-run
   processes, not containers on the compose network).

5. **`PromInstance.base_url` must be `https://` and non-local** (`src/domain/instances/entities.py`)
   — a deliberate security invariant, not a bug, mirroring a validator Plane itself uses for
   webhook URLs. It's real: `base_url` is on the hot path for every live work-item API call, not
   just cosmetic. Solved with a local self-signed-TLS nginx proxy
   (`plane-tls-proxy`, host `8443` → `host.docker.internal:8000`) using the public wildcard DNS
   service `nip.io` so the hostname (`plane.127.0.0.1.nip.io`) doesn't match the validator's
   localhost-looking blocklist, without needing `/etc/hosts` changes. Hub's own outbound `httpx`
   client verifies certs by default (correctly, no code change made to weaken this) — its
   containers were made to trust the self-signed cert by appending it to `certifi`'s bundle
   (`docker cp` + `cat >> $(python3 -c 'import certifi; print(certifi.where())')`). **This also
   doesn't persist** across container recreation.

6. **`discord-bot` restart-loops with zero installations** (logs
   `"no active Discord installations found — nothing to start, exiting"`, exits 0, `restart:
   unless-stopped` restarts it immediately) — a known Gate 6 limitation, not new. Left stopped;
   start it again (`docker start deployments-discord-bot-1`) once a real Discord installation
   exists.

## Tearing down

```bash
docker compose -f plane/docker-compose-local.yml -f plane/docker-compose-local.override.yml down
docker compose -f services/prom-integration-hub/deployments/docker-compose.yml down
docker rm -f plane-tls-proxy
# stop the pnpm dev processes (admin :3001, web :3010) with Ctrl-C / kill
```
Add `-v` to either `docker compose down` to also drop the Postgres volumes (full reset).
