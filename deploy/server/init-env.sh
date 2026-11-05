#!/usr/bin/env bash
# Creates the four server env files with fresh random secrets. Never overwrites an existing file,
# so it is safe to run again. Prints file names only, never secret values.
#
#   deploy/server/server.env               domains, ports, Hub DB password
#   plane/.env                             Plane compose vars (Postgres, RabbitMQ, MinIO)
#   plane/apps/api/.env                    Plane Django settings
#   services/prom-integration-hub/.env     Hub settings
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

rand() { openssl rand -hex "${1:-24}"; }
fernet() { openssl rand 32 | base64 | tr '+/' '-_'; }

created() { echo "created  $1"; }
kept() { echo "kept     $1 (already exists)"; }

# --- server.env -------------------------------------------------------------------------------
f="$HERE/server.env"
if [[ -f "$f" ]]; then kept "$f"; else
  sed "s/^HUB_DB_PASSWORD=.*/HUB_DB_PASSWORD=$(rand)/" "$HERE/server.env.example" > "$f"
  created "$f"
fi
# shellcheck disable=SC1090
set -a; . "$f"; set +a

# Shared values for the files below (only used when a file is being created).
PG_PASS="$(rand)"
MQ_PASS="$(rand)"
MINIO_KEY="prom$(rand 6)"
MINIO_SECRET="$(rand)"
HUB_TOKEN="$(rand 32)"
PLANE_URL="https://${PROM_DOMAIN}"
HUB_URL="https://${PROM_HUB_DOMAIN}"

# --- plane/.env ---------------------------------------------------------------------------------
f="$ROOT/plane/.env"
if [[ -f "$f" ]]; then kept "$f"; else
  cat > "$f" <<EOF
POSTGRES_USER=plane
POSTGRES_PASSWORD=${PG_PASS}
POSTGRES_DB=plane
PGDATA=/var/lib/postgresql/data

REDIS_HOST=plane-redis
REDIS_PORT=6379

RABBITMQ_HOST=plane-mq
RABBITMQ_PORT=5672
RABBITMQ_USER=plane
RABBITMQ_PASSWORD=${MQ_PASS}
RABBITMQ_VHOST=plane

AWS_REGION=
AWS_ACCESS_KEY_ID=${MINIO_KEY}
AWS_SECRET_ACCESS_KEY=${MINIO_SECRET}
AWS_S3_ENDPOINT_URL=http://plane-minio:9000
AWS_S3_BUCKET_NAME=uploads
FILE_SIZE_LIMIT=5242880
USE_MINIO=1
MINIO_ENDPOINT_SSL=1
API_KEY_RATE_LIMIT=60/minute
EOF
  created "$f"
fi

# --- plane/apps/api/.env ------------------------------------------------------------------------
f="$ROOT/plane/apps/api/.env"
if [[ -f "$f" ]]; then kept "$f"; else
  # Reuse the passwords already in plane/.env, in case only this file was missing.
  # shellcheck disable=SC1090
  set -a; . "$ROOT/plane/.env"; set +a
  cat > "$f" <<EOF
DEBUG=0
SECRET_KEY=$(rand 32)
CORS_ALLOWED_ORIGINS=${PLANE_URL}

POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_HOST=plane-db
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_PORT=5432
DATABASE_URL=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@plane-db:5432/${POSTGRES_DB}

REDIS_HOST=plane-redis
REDIS_PORT=6379
REDIS_URL=redis://plane-redis:6379/

RABBITMQ_HOST=plane-mq
RABBITMQ_PORT=5672
RABBITMQ_USER=${RABBITMQ_USER}
RABBITMQ_PASSWORD=${RABBITMQ_PASSWORD}
RABBITMQ_VHOST=${RABBITMQ_VHOST}

AWS_REGION=
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
AWS_S3_ENDPOINT_URL=http://plane-minio:9000
AWS_S3_BUCKET_NAME=uploads
FILE_SIZE_LIMIT=5242880
SIGNED_URL_EXPIRATION=3600
USE_MINIO=1
MINIO_ENDPOINT_SSL=1

WEB_URL=${PLANE_URL}
APP_BASE_URL=${PLANE_URL}
APP_BASE_PATH=
ADMIN_BASE_URL=${PLANE_URL}
ADMIN_BASE_PATH=/god-mode
SPACE_BASE_URL=${PLANE_URL}
SPACE_BASE_PATH=/spaces
LIVE_BASE_URL=${PLANE_URL}
LIVE_BASE_PATH=/live
LIVE_SERVER_SECRET_KEY=$(rand)

GUNICORN_WORKERS=2
HARD_DELETE_AFTER_DAYS=60
API_KEY_RATE_LIMIT=60/minute

# Plane -> Hub webhooks go through the host nginx (private IP via extra_hosts), so allow it.
WEBHOOK_ALLOWED_HOSTS=${PROM_HUB_DOMAIN}
WEBHOOK_ALLOWED_IPS=172.16.0.0/12

# Prom Hub bridge. PROM_INSTANCE_ID / PROM_CUSTOMER_ID are filled in after the Hub bootstrap
# step (README step 6), then: prom.sh plane up -d --force-recreate api worker beat-worker
PROM_HUB_BASE_URL=${HUB_URL}
PROM_HUB_SERVICE_TOKEN=${HUB_TOKEN}
PROM_INSTANCE_ID=
PROM_CUSTOMER_ID=
EOF
  created "$f"
fi

# --- services/prom-integration-hub/.env --------------------------------------------------------
f="$ROOT/services/prom-integration-hub/.env"
if [[ -f "$f" ]]; then kept "$f"; else
  # Must be the same token Plane sends.
  token="$(grep -E '^PROM_HUB_SERVICE_TOKEN=' "$ROOT/plane/apps/api/.env" | cut -d= -f2-)"
  cat > "$f" <<EOF
ENVIRONMENT=production
LOG_LEVEL=INFO

# Overridden inside the containers by the compose files; kept valid for completeness.
DATABASE_URL=postgresql+asyncpg://prom:${HUB_DB_PASSWORD}@postgres:5432/prom_hub
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

PROM_API_BASE_URL=${PLANE_URL}
SECRET_ENCRYPTION_KEY=$(fernet)
HUB_SERVICE_TOKEN=${token}
HUB_PUBLIC_BASE_URL=${HUB_URL}

# AI defaults. The key can also be set later in Plane: Settings -> AI Assistant.
AI_ENABLED=false
NT_AI_HUB_BASE_URL=https://aihub.ntaisolution.com/core-api/v1
NT_AI_HUB_KEY=
NT_AI_HUB_MODEL=gpt-oss-120b
AI_TIMEOUT_SECONDS=15
EOF
  created "$f"
fi

echo
echo "Done. Back up these files somewhere safe: SECRET_ENCRYPTION_KEY cannot be changed later"
echo "without losing every stored LINE/Discord credential."
