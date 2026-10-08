#!/bin/sh
# Local database backup: a compressed, custom-format dump of the whole application database.
# Usage: ./infra/backup/backup.sh [output-directory]
# The dump contains tenant data. Keep it out of version control (backups/ is git-ignored).
set -eu

OUT_DIR="${1:-backups}"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT_DIR/crm-$STAMP.dump"

docker compose exec -T db sh -c 'pg_dump --format=custom --no-owner --dbname="$POSTGRES_DB" --username="$POSTGRES_USER"' > "$FILE"
test -s "$FILE" || { echo "Backup failed: empty file" >&2; rm -f "$FILE"; exit 1; }
# Fail loudly on a truncated dump instead of discovering it during a restore.
docker compose exec -T db pg_restore --list < "$FILE" > /dev/null
echo "$FILE"
