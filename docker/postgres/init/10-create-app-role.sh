#!/bin/bash
# Creates the role Django actually connects with.
#
# This exists because Postgres exempts two kinds of role from Row-Level
# Security no matter what policies are written: superusers, and roles with
# BYPASSRLS. Tenant isolation in this system is enforced by RLS, so the
# application must never connect as either. The superuser created by the
# Postgres image is used here, at build time, and then never again.
#
# CREATEDB is granted because the test suite creates a throwaway database, and
# it must be owned by this same role so that RLS behaves identically under test
# as it does in production. Testing isolation as a role that can bypass
# isolation would prove nothing.
#
# Runs once, on an empty data directory.

set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     <<-EOSQL
    CREATE ROLE ${APP_DB_USER} WITH
        LOGIN
        PASSWORD '${APP_DB_PASSWORD}'
        NOSUPERUSER
        NOBYPASSRLS
        NOCREATEROLE
        CREATEDB;

    -- The application role owns the database and the public schema so that
    -- migrations can create tables. Ownership alone would normally let it read
    -- past RLS policies, which is why every tenant-owned table is created with
    -- FORCE ROW LEVEL SECURITY. See apps/core/db/rls.py.
    ALTER DATABASE ${POSTGRES_DB} OWNER TO ${APP_DB_USER};
    ALTER SCHEMA public OWNER TO ${APP_DB_USER};
    GRANT ALL ON SCHEMA public TO ${APP_DB_USER};
EOSQL

echo "Created application role ${APP_DB_USER} (NOSUPERUSER, NOBYPASSRLS)."
