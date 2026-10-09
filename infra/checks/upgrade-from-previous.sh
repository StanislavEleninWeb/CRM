#!/bin/sh
# Upgrade test: a database at an earlier revision, holding data, is migrated to the current head.
#   ./infra/checks/upgrade-from-previous.sh [from-revision]     (default: 0008, the first published revision)
# Uses a scratch database in the local stack and removes it afterwards.
set -eu

FROM="${1:-0008}"
SCRATCH="crm_upgrade_check"
run() { docker compose exec -T db sh -c "$1"; }
psql_scratch() { docker compose exec -T db sh -c "psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align --username=\"\$POSTGRES_USER\" --dbname=$SCRATCH -c \"$1\""; }
cleanup() { run "psql --quiet --username=\"\$POSTGRES_USER\" --dbname=postgres -c 'DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)'" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
run "sh /docker-entrypoint-initdb.d/01-roles.sh $SCRATCH" >/dev/null

alembic_on_scratch() {
  docker compose run --rm -T api sh -c "export MIGRATION_DATABASE_URL=\$(echo \"\$MIGRATION_DATABASE_URL\" | sed 's#/[^/]*\$#/$SCRATCH#'); alembic $1" >/dev/null 2>&1
}

echo "1. Create the schema at revision $FROM"
alembic_on_scratch "upgrade $FROM"
echo "2. Put data in it, as the earlier release would have"
psql_scratch "INSERT INTO users (id, email, display_name, oidc_issuer, oidc_subject) VALUES ('00000000-0000-0000-0000-0000000000a1', 'earlier@example.test', 'Earlier User', 'https://issuer.example', 'sub-1')" >/dev/null
psql_scratch "SELECT set_config('app.user_id', '00000000-0000-0000-0000-0000000000a1', false); SELECT tenant_create('Workspace from the earlier release')" >/dev/null
TENANT="$(psql_scratch "SELECT id FROM tenants LIMIT 1")"
psql_scratch "SELECT set_config('app.tenant_id', '$TENANT', false); INSERT INTO companies (tenant_id, name) VALUES ('$TENANT', 'Company kept through the upgrade')" >/dev/null
echo "3. Upgrade to the current head"
alembic_on_scratch "upgrade head"
echo "4. The data is still there and the new structures exist"
[ "$(psql_scratch "SELECT set_config('app.tenant_id', '$TENANT', false); SELECT name FROM companies" | tail -1)" = "Company kept through the upgrade" ] || { echo "FAIL: data was lost" >&2; exit 1; }
[ "$(psql_scratch "SELECT count(*) FROM due_jobs WHERE kind = 'retention.purge' AND tenant_id = '$TENANT'")" = "1" ] || { echo "FAIL: the existing workspace did not get its retention job" >&2; exit 1; }
[ "$(psql_scratch "SELECT count(*) FROM plans")" -ge 3 ] || { echo "FAIL: plans were not seeded" >&2; exit 1; }
HEAD="$(psql_scratch "SELECT version_num FROM alembic_version")"
echo "Upgrade check passed: $FROM -> $HEAD with existing data"
