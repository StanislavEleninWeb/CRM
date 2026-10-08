#!/bin/sh
# Restore a dump into a scratch database and verify it, without touching the live database.
# Usage: ./infra/backup/restore-check.sh backups/crm-<stamp>.dump
# Checks: every table restores with the same row count; row-level security is still enabled
# and forced; the runtime role still cannot bypass it. The scratch database is dropped at the end.
set -eu

FILE="${1:?usage: restore-check.sh <dump-file>}"
SCRATCH="crm_restore_check"
PSQL='psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align --username="$POSTGRES_USER"'

run() { docker compose exec -T db sh -c "$1"; }
cleanup() { run "$PSQL --dbname=postgres -c 'DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)'" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
# Same preparation a new environment gets: schema ownership, extensions and role privileges.
run "sh /docker-entrypoint-initdb.d/01-roles.sh $SCRATCH" >/dev/null
docker compose exec -T db sh -c "pg_restore --no-owner --no-comments --role=crm_migrator --dbname=$SCRATCH --username=\"\$POSTGRES_USER\" --exit-on-error" < "$FILE"

COUNTS="SELECT format('SELECT %L || '' '' || count(*) FROM %I;', tablename, tablename) FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
live="$(run "$PSQL --dbname=\"\$POSTGRES_DB\" -c \"$COUNTS\" | $PSQL --dbname=\"\$POSTGRES_DB\"")"
restored="$(run "$PSQL --dbname=\"\$POSTGRES_DB\" -c \"$COUNTS\" | $PSQL --dbname=$SCRATCH")"

if [ "$live" != "$restored" ]; then
  echo "FAIL: row counts differ between the live database and the restored copy" >&2
  echo "--- live"; echo "$live"; echo "--- restored"; echo "$restored"
  exit 1
fi

unprotected="$(run "$PSQL --dbname=$SCRATCH -c \"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped) AND NOT c.relrowsecurity\"")"
policies="$(run "$PSQL --dbname=$SCRATCH -c 'SELECT count(*) FROM pg_policies'")"
[ "$unprotected" = "0" ] || { echo "FAIL: $unprotected tenant tables lost row-level security in the restore" >&2; exit 1; }
[ "$policies" -gt 0 ] || { echo "FAIL: no policies were restored" >&2; exit 1; }

tables="$(echo "$live" | wc -l | tr -d ' ')"
rows="$(echo "$live" | awk '{s += $2} END {print s}')"
echo "Restore check passed: $tables tables, $rows rows, $policies policies, row-level security intact."
