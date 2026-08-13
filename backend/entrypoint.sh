#!/bin/sh
# Applies migrations and seeds a platform admin before handing over to the
# real command. Keeping this here is what makes `docker compose up` the single
# setup step: a clean checkout reaches a usable API without anyone running
# migrate by hand and without a second set of instructions to keep in sync.

set -e

echo "Waiting for Postgres at ${DB_HOST}:${DB_PORT}..."
until pg_isready -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -q; do
    sleep 1
done

echo "Applying migrations..."
python manage.py migrate --no-input

echo "Ensuring platform admin exists..."
python manage.py ensure_platform_admin

exec "$@"
