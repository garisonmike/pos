#!/bin/sh
# Take a backup of everything that cannot be rebuilt.
#
# Two things go in: the database, and the media directory. Everything else -
# code, images, static files - is reproducible from the repository, and backing
# it up would only make the restore slower to read.
#
# `pg_dump -Fc` rather than plain SQL, because the custom format restores
# selectively, in parallel, and refuses to load into an incompatible server
# rather than half-loading and stopping.
#
# **A backup nobody has restored is a hope.** See restore.sh, and the drill in
# DEPLOYMENT.md. This script is the easy half.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
MEDIA_DIR="${MEDIA_DIR:-/srv/media}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"

stamp=$(date +%Y%m%d-%H%M%S)
weekday=$(date +%u)

mkdir -p "${BACKUP_DIR}/daily" "${BACKUP_DIR}/weekly"

db_file="${BACKUP_DIR}/daily/pos-${stamp}.dump"
media_file="${BACKUP_DIR}/daily/media-${stamp}.tar.gz"

echo "Dumping ${PGDATABASE} from ${PGHOST}..."
# --clean --if-exists so the dump can be restored over an existing database
# without a manual drop first, which is one fewer step to get wrong at the
# moment somebody is already having a bad day.
pg_dump --format=custom --clean --if-exists --file="${db_file}"

if [ ! -s "${db_file}" ]; then
    echo "Dump file is empty. Refusing to call this a backup." >&2
    rm -f "${db_file}"
    exit 1
fi

# Read it back before believing it. pg_restore --list parses the archive's
# table of contents, so a truncated or corrupt dump fails here rather than
# during a restore six weeks from now.
if ! pg_restore --list "${db_file}" > /dev/null 2>&1; then
    echo "Dump is not readable by pg_restore. Refusing to keep it." >&2
    rm -f "${db_file}"
    exit 1
fi

table_count=$(pg_restore --list "${db_file}" | grep -c "TABLE DATA" || true)
echo "Dump written: ${db_file} ($(du -h "${db_file}" | cut -f1), ${table_count} tables with data)"

if [ -d "${MEDIA_DIR}" ]; then
    tar -czf "${media_file}" -C "${MEDIA_DIR}" . 2>/dev/null || true
    echo "Media written: ${media_file} ($(du -h "${media_file}" | cut -f1))"
fi

# Monday's copy is kept as the weekly. A hardlink rather than a second dump, so
# retention costs nothing until the daily is pruned.
if [ "${weekday}" = "1" ]; then
    ln -f "${db_file}" "${BACKUP_DIR}/weekly/pos-${stamp}.dump"
    [ -f "${media_file}" ] && ln -f "${media_file}" "${BACKUP_DIR}/weekly/media-${stamp}.tar.gz"
    echo "Kept as this week's weekly copy."
fi

prune() {
    directory="$1"
    pattern="$2"
    keep="$3"
    # shellcheck disable=SC2012
    ls -1t "${directory}"/${pattern} 2>/dev/null | tail -n "+$((keep + 1))" | while read -r old; do
        echo "Pruning ${old}"
        rm -f "${old}"
    done
}

prune "${BACKUP_DIR}/daily" "pos-*.dump" "${KEEP_DAILY}"
prune "${BACKUP_DIR}/daily" "media-*.tar.gz" "${KEEP_DAILY}"
prune "${BACKUP_DIR}/weekly" "pos-*.dump" "${KEEP_WEEKLY}"
prune "${BACKUP_DIR}/weekly" "media-*.tar.gz" "${KEEP_WEEKLY}"

echo "Backup complete: $(ls -1 "${BACKUP_DIR}"/daily/pos-*.dump 2>/dev/null | wc -l) daily, $(ls -1 "${BACKUP_DIR}"/weekly/pos-*.dump 2>/dev/null | wc -l) weekly."
