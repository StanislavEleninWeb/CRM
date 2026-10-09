#!/bin/sh
# Restore a dump into a scratch database and verify it, without touching the live database.
# Usage: ./infra/backup/restore-check.sh backups/crm-<stamp>.dump[.enc]
# Checks: every table restores with the same row count; row-level security is still enabled
# and forced; the runtime role still cannot bypass it. The scratch database is dropped at the end.
set -eu

FILE="${1:?usage: restore-check.sh <dump-file | dump-file.enc>}"
# An encrypted backup is decrypted to a temporary file that is removed afterwards.
case "$FILE" in
  *.enc)
    : "${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required to read an encrypted backup}"
    if [ -f "$FILE.sha256" ]; then
      ( cd "$(dirname "$FILE")" && { command -v sha256sum >/dev/null 2>&1 && sha256sum -c "$(basename "$FILE").sha256" || shasum -a 256 -c "$(basename "$FILE").sha256"; } >/dev/null ) \
        || { echo "FAIL: the backup file does not match its checksum" >&2; exit 1; }
    fi
    DECRYPTED="$(mktemp "${TMPDIR:-/tmp}/crm-restore.XXXXXX")"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass "file:$BACKUP_PASSPHRASE_FILE" -in "$FILE" -out "$DECRYPTED" \
      || { rm -f "$DECRYPTED"; echo "FAIL: the backup could not be decrypted" >&2; exit 1; }
    FILE="$DECRYPTED"
    ;;
esac
SCRATCH="crm_restore_check"
PSQL='psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align --username="$POSTGRES_USER"'

run() { docker compose exec -T db sh -c "$1"; }
drop_scratch() { run "$PSQL --dbname=postgres -c 'DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)'" >/dev/null 2>&1 || true; }
cleanup() {
  drop_scratch
  [ -z "${DECRYPTED:-}" ] || rm -f "$DECRYPTED"
}
trap cleanup EXIT

drop_scratch
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
if [ "$unprotected" != "0" ]; then
  names="$(run "$PSQL --dbname=$SCRATCH -c \"SELECT string_agg(c.relname, ', ') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped) AND NOT c.relrowsecurity\"")"
  echo "FAIL: $unprotected tenant tables have no row-level security in the restore: $names" >&2
  exit 1
fi
[ "$policies" -gt 0 ] || { echo "FAIL: no policies were restored" >&2; exit 1; }

tables="$(echo "$live" | wc -l | tr -d ' ')"
rows="$(echo "$live" | awk '{s += $2} END {print s}')"
echo "Restore check passed: $tables tables, $rows rows, $policies policies, row-level security intact."
