#!/usr/bin/env bash
# The ONLY way to run docker compose for Prom on the shared server.
#
#   deploy/server/prom.sh plane <compose args...>
#   deploy/server/prom.sh hub   <compose args...>
#
# Always pins the project name (-p prom-plane / prom-hub), the compose files and the env files,
# so a command can never touch another user's stack. Volume-deleting flags are refused.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER_ENV="$HERE/server.env"

usage() {
  echo "usage: $0 {plane|hub} <docker compose args>" >&2
  echo "  e.g. $0 hub ps    |  $0 plane logs -f api    |  $0 hub up -d api worker beat" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
stack="$1"
shift

[[ -f "$SERVER_ENV" ]] || { echo "missing $SERVER_ENV — run deploy/server/init-env.sh first" >&2; exit 1; }

case "$stack" in
  plane)
    [[ -f "$ROOT/plane/.env" && -f "$ROOT/plane/apps/api/.env" ]] \
      || { echo "missing plane/.env or plane/apps/api/.env — run init-env.sh" >&2; exit 1; }
    args=(-p prom-plane
          --project-directory "$ROOT/plane"
          -f "$ROOT/plane/docker-compose.yml"
          -f "$HERE/plane.override.yml"
          --env-file "$ROOT/plane/.env"
          --env-file "$SERVER_ENV")
    ;;
  hub)
    hub_dir="$ROOT/services/prom-integration-hub"
    [[ -f "$hub_dir/.env" ]] || { echo "missing $hub_dir/.env — run init-env.sh" >&2; exit 1; }
    args=(-p prom-hub
          --project-directory "$hub_dir/deployments"
          -f "$hub_dir/deployments/docker-compose.yml"
          -f "$HERE/hub.override.yml"
          --env-file "$SERVER_ENV")
    ;;
  *) usage ;;
esac

# Refuse anything that deletes volumes or images, even inside our own project.
for a in "$@"; do
  case "$a" in
    -v|--volumes|--volumes=*|--rmi|--rmi=*|prune)
      echo "refused: '$a' deletes data. Do it by hand only after a backup and a second look." >&2
      exit 1
      ;;
  esac
done

if [[ "${1:-}" == "down" ]]; then
  read -r -p "This stops and removes ALL prom-$stack containers (volumes are kept). Type '$stack' to continue: " ok
  [[ "$ok" == "$stack" ]] || { echo "cancelled"; exit 1; }
fi

# praew is not in the docker group yet; fall back to sudo (password typed by the user).
if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo docker)
fi

set -x
exec "${DOCKER[@]}" compose "${args[@]}" "$@"
