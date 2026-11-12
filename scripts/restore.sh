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
MEDIA_BACKUP_REQUIRED="${MEDIA_BACKUP_REQUIRED:-true}"

if [ "${MEDIA_BACKUP_REQUIRED}" != "true" ] && [ "${MEDIA_BACKUP_REQUIRED}" != "false" ]; then
    echo "MEDIA_BACKUP_REQUIRED must be true or false (got ${MEDIA_BACKUP_REQUIRED})." >&2
    exit 2
fi

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

    dropdb --if-exists "${SCRATCH_DB}"
    createdb "${SCRATCH_DB}"

    # --no-owner and --no-privileges: the scratch database has no application
    # role, and a drill should not need one created to answer the question it
    # is asking. Capture output to a file so the restore's exit status is not
    # lost behind grep/head in a pipeline (POSIX sh reports the last command's
    # status, which could make a failed restore look successful).
    restore_log="${TMPDIR:-/tmp}/pos-restore-${SCRATCH_DB}-$$.log"
    if ! pg_restore --dbname="${SCRATCH_DB}" --no-owner --no-privileges "${dump}" >"${restore_log}" 2>&1; then
        sed -n '1,40p' "${restore_log}" >&2
        rm -f "${restore_log}"
        echo "DRILL FAILED: pg_restore could not restore the dump." >&2
        exit 1
    fi
    sed -n '1,20p' "${restore_log}"
    rm -f "${restore_log}"

    echo
    report "${SCRATCH_DB}" "Restored from dump"
    echo

    # The current database is not a valid completeness baseline: records may
    # have been edited or deleted since this older snapshot. Check that core
    # tables are queryable and report their restored counts as a limited
    # sanity check only. A successful restore plus these checks is not proof
    # that every expected row or media file is present.
    for table in accounts_user catalog_item sales_sale sales_payment compliance_document; do
        restored_count=$(count_in "${SCRATCH_DB}" "${table}")
        case "${restored_count}" in
            *[!0-9]*|"")
                echo "DRILL FAILED: could not query restored row count for ${table} (got ${restored_count})." >&2
                exit 1
                ;;
        esac
        echo "Rows ${table}: restored ${restored_count} (sanity check only)."
    done

    stamp=${dump##*/pos-}
    stamp=${stamp%.dump}
    dump_dir=${dump%/*}
    [ "${dump_dir}" = "${dump}" ] && dump_dir=.
    media_archive="${dump_dir}/media-${stamp}.tar.gz"
    if [ -f "${media_archive}" ]; then
        media_drill_dir=$(mktemp -d "${TMPDIR:-/tmp}/pos-media-drill.XXXXXX")
        if ! tar -tzf "${media_archive}" >/dev/null 2>&1 || \
            ! tar -xzf "${media_archive}" -C "${media_drill_dir}"; then
            rm -rf "${media_drill_dir}"
            echo "DRILL FAILED: media archive could not be read and extracted." >&2
            exit 1
        fi
        archive_entries=$(tar -tzf "${media_archive}" | wc -l | tr -d ' ')
        restored_entries=$(find "${media_drill_dir}" -mindepth 1 | wc -l | tr -d ' ')
        expected_entries=$((archive_entries - 1)) # tar's root entry is '.'
        if [ "${restored_entries}" != "${expected_entries}" ]; then
            rm -rf "${media_drill_dir}"
            echo "DRILL FAILED: media archive contains ${expected_entries} entries but ${restored_entries} were restored." >&2
            exit 1
        fi
        rm -rf "${media_drill_dir}"
        echo "Media:   archive read and restored (${restored_entries} entries)."
    elif [ "${MEDIA_BACKUP_REQUIRED}" = "true" ]; then
        echo "DRILL FAILED: required media archive ${media_archive} is missing." >&2
        exit 1
    else
        echo "WARNING: no media archive accompanies this database dump; media backup is configured as optional." >&2
    fi

    echo "DRILL PASSED. The dump restores and the matching media archive was read."
    echo "Row counts are limited sanity checks; they do not prove snapshot completeness."
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
