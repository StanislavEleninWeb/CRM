#!/bin/sh
# Runs once, as the bootstrap superuser, when the data directory is empty.
# Can also be run later with database names as arguments to prepare additional databases.
# Creates the two roles the application uses:
#   crm_migrator - owns schema objects and runs migrations
#   crm_app      - runtime role: no superuser, no BYPASSRLS, no DDL
set -eu

: "${MIGRATOR_PASSWORD:?MIGRATOR_PASSWORD is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"

create_roles() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v migrator_password="$MIGRATOR_PASSWORD" -v app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE crm_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_migrator') \gexec
SELECT format('CREATE ROLE crm_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app') \gexec
SQL
}

prepare_database() {
  db="$1"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v db="$db" <<'SQL'
SELECT format('CREATE DATABASE %I', :'db')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO crm_migrator;
GRANT USAGE ON SCHEMA public TO crm_app;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
-- Everything the migrator creates later is usable, but not alterable, by the runtime role.
ALTER DEFAULT PRIVILEGES FOR ROLE crm_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE crm_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO crm_app;
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v db="$db" <<'SQL'
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO crm_migrator, crm_app', :'db') \gexec
-- The migrator may create schemas and trusted extensions; the runtime role may not.
SELECT format('GRANT CREATE ON DATABASE %I TO crm_migrator', :'db') \gexec
SQL
}

create_roles
if [ "$#" -gt 0 ]; then
  # Called by hand, for example to prepare a scratch database before a restore.
  for database in "$@"; do prepare_database "$database"; done
else
  prepare_database "$POSTGRES_DB"
  prepare_database "${POSTGRES_DB}_test"
fi
