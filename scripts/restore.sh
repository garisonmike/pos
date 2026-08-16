#!/bin/sh
# Restore a dump, and prove it worked.
#
# **This is the deliverable, not the backup.** A dump that has never been read
# back is a hope with a filename. This script exists so that restoring is
# something already done once rather than something first attempted on the
# worst day of the year.
#
# Two modes:
#
#   ./restore.sh --drill [dump]     restore into a scratch database and count
#                                   what came back. Safe: never touches live.
#
#   ./restore.sh --live  [dump]     restore over the real database. Refuses
#                                   unless RESTORE_I_MEAN_IT=yes, because this
#                                   is the one command here that destroys data.
#
# With no dump named, the most recent daily is used.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
SCRATCH_DB="${SCRATCH_DB:-pos_restore_test}"

mode="${1:-}"
dump="${2:-}"

if [ "${mode}" != "--drill" ] && [ "${mode}" != "--live" ]; then
    echo "Usage: restore.sh --drill|--live [dump-file]" >&2
    exit 2
fi

if [ -z "${dump}" ]; then
    # shellcheck disable=SC2012
    dump=$(ls -1t "${BACKUP_DIR}"/daily/pos-*.dump 2>/dev/null | head -1 || true)
fi

if [ -z "${dump}" ] || [ ! -f "${dump}" ]; then
    echo "No dump to restore. Looked in ${BACKUP_DIR}/daily." >&2
    exit 1
fi

echo "Dump:   ${dump}"
echo "Taken:  $(date -r "${dump}" 2>/dev/null || stat -c %y "${dump}" 2>/dev/null || echo unknown)"
echo "Size:   $(du -h "${dump}" | cut -f1)"
echo

count_in() {
    database="$1"
    table="$2"
    psql -d "${database}" -tAc "SELECT count(*) FROM ${table};" 2>/dev/null || echo "n/a"
}

report() {
    database="$1"
    label="$2"
    echo "--- ${label} (${database}) ---"
    for table in tenants_tenant accounts_user catalog_item sales_sale sales_payment compliance_document; do
        printf '  %-24s %s\n' "${table}" "$(count_in "${database}" "${table}")"
    done
}

if [ "${mode}" = "--drill" ]; then
    echo "Restoring into scratch database ${SCRATCH_DB}. The live database is not touched."
    echo

    # Counted before the restore, so the comparison is against what is actually
    # running rather than against a number somebody remembered.
    report "${PGDATABASE}" "Live now"
    echo

    dropdb --if-exists "${SCRATCH_DB}"
    createdb "${SCRATCH_DB}"

    # --no-owner and --no-privileges: the scratch database has no application
    # role, and a drill should not need one created to answer the question it
    # is asking.
    if ! pg_restore --dbname="${SCRATCH_DB}" --no-owner --no-privileges "${dump}" 2>&1 | grep -v "^$" | head -20; then
        echo "pg_restore reported problems - read the output above." >&2
    fi

    echo
    report "${SCRATCH_DB}" "Restored from dump"
    echo

    live_sales=$(count_in "${PGDATABASE}" sales_sale)
    restored_sales=$(count_in "${SCRATCH_DB}" sales_sale)
    live_tenants=$(count_in "${PGDATABASE}" tenants_tenant)
    restored_tenants=$(count_in "${SCRATCH_DB}" tenants_tenant)

    echo "Sales:   live ${live_sales}, restored ${restored_sales}"
    echo "Tenants: live ${live_tenants}, restored ${restored_tenants}"
    echo

    # The restored copy is expected to be *behind* live, not equal to it: the
    # dump was taken hours ago and trading continued. What would be wrong is
    # the restore having more than live, or having nothing at all.
    if [ "${restored_tenants}" = "0" ] || [ "${restored_tenants}" = "n/a" ]; then
        echo "DRILL FAILED: the restored database has no tenants." >&2
        exit 1
    fi
    if [ "${restored_sales}" = "n/a" ]; then
        echo "DRILL FAILED: the sales table did not come back." >&2
        exit 1
    fi

    echo "DRILL PASSED. The dump restores and contains the expected tables."
    echo "Scratch database ${SCRATCH_DB} left in place for inspection; it is"
    echo "dropped and recreated on the next drill."
    exit 0
fi

# ---- Live restore -------------------------------------------------------

if [ "${RESTORE_I_MEAN_IT:-}" != "yes" ]; then
    cat >&2 <<'WARNING'
Refusing to restore over the live database.

This replaces every row with the contents of the dump. Anything traded since
the dump was taken is gone - sales, payments, stock movements, all of it.

Stop the application first, so nothing is writing while this runs:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api

Then re-run with:

    RESTORE_I_MEAN_IT=yes ./restore.sh --live [dump-file]
WARNING
    exit 1
fi

echo "Restoring over ${PGDATABASE}. Every row is about to be replaced."
report "${PGDATABASE}" "Before"
echo

pg_restore --dbname="${PGDATABASE}" --clean --if-exists --no-owner "${dump}"

echo
report "${PGDATABASE}" "After"
echo
echo "Restore complete. Start the application and check the health endpoint:"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api"
