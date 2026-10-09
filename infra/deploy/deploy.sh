#!/bin/sh
# Deploy one release to the host this script runs on. Run by the release workflow over SSH, or by hand.
#
#   API_IMAGE=ghcr.io/owner/crm-api@sha256:...  FRONTEND_IMAGE=ghcr.io/owner/crm-frontend@sha256:...  ./deploy.sh
#
# Order: record what is running, take a backup, run migrations as a one-off job, replace the
# services, wait for health. Any failure stops the deployment and leaves the previous containers
# running. Rolling back is running this again with the previous image references (written to
# previous-release.env), which is safe only for schema changes that the previous code tolerates:
# see docs/runbooks/deployment.md.
set -eu

: "${API_IMAGE:?API_IMAGE is required (an image reference with a digest)}"
: "${FRONTEND_IMAGE:?FRONTEND_IMAGE is required (an image reference with a digest)}"
DIR="$(cd "$(dirname "$0")/../production" && pwd)"
STATE_DIR="${STATE_DIR:-/var/lib/seweb-crm}"
PROFILE_ARGS="${COMPOSE_PROFILES_ARGS:---profile proxy}"
COMPOSE="docker compose -f $DIR/compose.yaml ${COMPOSE_EXTRA:-}"

case "$API_IMAGE$FRONTEND_IMAGE" in
  *@sha256:*@sha256:*) ;;
  *) [ "${ALLOW_TAGS:-}" = "local-check" ] || { echo "Refusing to deploy: both images must be pinned by digest." >&2; exit 1; } ;;
esac
export API_IMAGE FRONTEND_IMAGE

mkdir -p "$STATE_DIR"
if [ -f "$STATE_DIR/current-release.env" ]; then cp "$STATE_DIR/current-release.env" "$STATE_DIR/previous-release.env"; fi

echo "1. Validate the configuration"
$COMPOSE $PROFILE_ARGS --profile release config --quiet

echo "2. Pull the images"
[ "${SKIP_PULL:-}" = "1" ] || $COMPOSE $PROFILE_ARGS --profile release pull --quiet api frontend

echo "3. Start the database and queue if they are not running"
$COMPOSE up -d --wait db redis

if [ -n "${BACKUP_COMMAND:-}" ]; then
  echo "4. Back up before changing the schema"
  sh -c "$BACKUP_COMMAND"
else
  echo "4. No BACKUP_COMMAND set: skipping the pre-migration backup"
fi

echo "5. Run migrations as a one-off job"
$COMPOSE --profile release run --rm migrate

echo "6. Replace the services and wait until they are healthy"
$COMPOSE $PROFILE_ARGS up -d --wait --remove-orphans api worker scheduler frontend ${START_PROXY:-proxy}

printf 'API_IMAGE=%s\nFRONTEND_IMAGE=%s\nDEPLOYED_AT=%s\n' "$API_IMAGE" "$FRONTEND_IMAGE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE_DIR/current-release.env"
echo "Deployed. Previous release, if any, is recorded in $STATE_DIR/previous-release.env"
