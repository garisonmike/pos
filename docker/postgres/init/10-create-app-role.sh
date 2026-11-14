#!/bin/bash
# Creates the role Django actually connects with.
#
# This exists because Postgres exempts two kinds of role from Row-Level
# Security no matter what policies are written: superusers, and roles with
# BYPASSRLS. Tenant isolation in this system is enforced by RLS, so the
# application must never connect as either. The superuser created by the
# Postgres image is used here, at build time, and then never again.
#
# Runtime cannot create databases. Development/CI can optionally create a
# separate NOSUPERUSER/NOBYPASSRLS test role with CREATEDB for isolated tests.
#
# Runs once, on an empty data directory.

set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --set=app_role="$APP_DB_USER" \
     --set=app_password="$APP_DB_PASSWORD" \
     --set=db_name="$POSTGRES_DB" \
     <<-EOSQL
    CREATE ROLE :"app_role" WITH
        LOGIN
        PASSWORD :'app_password'
        NOSUPERUSER
        NOBYPASSRLS
        NOCREATEROLE
        NOCREATEDB;

    -- The application role owns the database and the public schema so that
    -- migrations can create tables. Ownership alone would normally let it read
    -- past RLS policies, which is why every tenant-owned table is created with
    -- FORCE ROW LEVEL SECURITY. See apps/core/db/rls.py.
    ALTER DATABASE :"db_name" OWNER TO :"app_role";
    ALTER SCHEMA public OWNER TO :"app_role";
    GRANT ALL ON SCHEMA public TO :"app_role";
EOSQL

if [[ -n "${APP_TEST_DB_USER:-}" ]]; then
    psql -v ON_ERROR_STOP=1 \
         --username "$POSTGRES_USER" \
         --dbname "$POSTGRES_DB" \
         --set=test_role="$APP_TEST_DB_USER" \
         --set=test_password="${APP_TEST_DB_PASSWORD:-}" \
         --set=db_name="$POSTGRES_DB" \
         <<-EOSQL
        CREATE ROLE :"test_role" WITH
            LOGIN
            PASSWORD :'test_password'
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEROLE
            CREATEDB;
        GRANT CONNECT ON DATABASE :"db_name" TO :"test_role";
        GRANT USAGE, CREATE ON SCHEMA public TO :"test_role";
EOSQL
    echo "Created test role ${APP_TEST_DB_USER} (NOSUPERUSER, NOBYPASSRLS, CREATEDB)."
fi

echo "Created runtime role ${APP_DB_USER} (NOSUPERUSER, NOBYPASSRLS, NOCREATEDB)."
