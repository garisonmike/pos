#!/usr/bin/env bash
# Remove the test-only CREATEDB privilege from an existing runtime account.
# Postgres initialization scripts do not rerun against an existing volume.

set -euo pipefail

# Compose options (such as --env-file and -f) may be passed through unchanged.
docker compose "$@" exec -T db sh -s <<'REMOTE'
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --set=app_role="$APP_DB_USER" <<'SQL'
ALTER ROLE :"app_role" NOCREATEDB;
SQL

result=$(psql -v ON_ERROR_STOP=1 -At \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --set=app_role="$APP_DB_USER" <<'SQL'
SELECT rolcreatedb FROM pg_roles WHERE rolname = :'app_role';
SQL
)
test "$result" = f || { echo "Runtime role still has CREATEDB or is missing" >&2; exit 1; }

if test -n "${APP_TEST_DB_USER:-}"; then
    psql -v ON_ERROR_STOP=1 \
         --username "$POSTGRES_USER" \
         --dbname "$POSTGRES_DB" \
         --set=test_role="$APP_TEST_DB_USER" \
         --set=test_password="$APP_TEST_DB_PASSWORD" \
         --set=db_name="$POSTGRES_DB" <<'SQL'
SELECT format(
    'CREATE ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB',
    :'test_role', :'test_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'test_role')
\gexec
ALTER ROLE :"test_role" WITH LOGIN PASSWORD :'test_password' NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB;
GRANT CONNECT ON DATABASE :"db_name" TO :"test_role";
GRANT USAGE, CREATE ON SCHEMA public TO :"test_role";
SQL
fi
REMOTE
