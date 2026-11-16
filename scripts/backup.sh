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
MEDIA_BACKUP_REQUIRED="${MEDIA_BACKUP_REQUIRED:-true}"
BACKUP_REMOTE_REQUIRED="${BACKUP_REMOTE_REQUIRED:-false}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
BACKUP_ALERT_REQUIRED="${BACKUP_ALERT_REQUIRED:-false}"
BACKUP_ALERT_URL="${BACKUP_ALERT_URL:-}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"

if [ "${MEDIA_BACKUP_REQUIRED}" != "true" ] && [ "${MEDIA_BACKUP_REQUIRED}" != "false" ]; then
    echo "MEDIA_BACKUP_REQUIRED must be true or false (got ${MEDIA_BACKUP_REQUIRED})." >&2
    exit 2
fi

notify() {
    message="$1"
    if [ -z "${BACKUP_ALERT_URL}" ]; then
        if [ "${BACKUP_ALERT_REQUIRED}" = "true" ]; then
            echo "WARNING: BACKUP_ALERT_URL is empty; no backup notification was sent." >&2
            return 1
        fi
        return 0
    fi
    curl --fail --silent --show-error --max-time 15 \
        --request POST --header 'Content-Type: text/plain' \
        --data-binary "${message}" "${BACKUP_ALERT_URL}" >/dev/null
}

configuration_failure() {
    echo "$1" >&2
    case "${BACKUP_ALERT_URL}" in
        https://*)
            curl --fail --silent --show-error --max-time 15 \
                --request POST --header 'Content-Type: text/plain' \
                --data-binary "POS backup service could not run; inspect backup logs." \
                "${BACKUP_ALERT_URL}" >/dev/null \
                || echo "WARNING: configuration failure alert could not be delivered." >&2
            ;;
    esac
    exit 2
}

is_positive_integer() {
    case "$1" in
        [1-9]|[1-9][0-9]|[1-9][0-9][0-9]|[1-9][0-9][0-9][0-9]) return 0 ;;
        *) return 1 ;;
    esac
}

finish() {
    result=$?
    rm -f "${db_tmp:-}" "${media_tmp:-}"
    if [ "${result}" -eq 0 ]; then
        touch "${BACKUP_DIR}/.last_success"
        notify "POS backup completed at ${stamp}; local and configured remote copies verified." \
            || echo "WARNING: backup completed but its success alert could not be delivered." >&2
    else
        echo "!!! BACKUP FAILED at $(date) - exit ${result} !!!" >&2
        notify "POS backup failed at $(date); inspect backup service logs." \
            || echo "WARNING: backup failure alert could not be delivered." >&2
    fi
    exit "${result}"
}

if [ "${BACKUP_REMOTE_REQUIRED}" != "true" ] && [ "${BACKUP_REMOTE_REQUIRED}" != "false" ]; then
    configuration_failure "BACKUP_REMOTE_REQUIRED must be true or false (got ${BACKUP_REMOTE_REQUIRED})."
fi
if [ "${BACKUP_ALERT_REQUIRED}" != "true" ] && [ "${BACKUP_ALERT_REQUIRED}" != "false" ]; then
    configuration_failure "BACKUP_ALERT_REQUIRED must be true or false (got ${BACKUP_ALERT_REQUIRED})."
fi
for retention in "${KEEP_DAILY}" "${KEEP_WEEKLY}"; do
    is_positive_integer "${retention}" \
        || configuration_failure "Local backup retention must be a positive whole number no greater than 9999."
done
if [ "${BACKUP_REMOTE_REQUIRED}" = "true" ] && [ -z "${BACKUP_REMOTE}" ]; then
    configuration_failure "BACKUP_REMOTE is required when BACKUP_REMOTE_REQUIRED=true."
fi
if [ "${BACKUP_REMOTE_REQUIRED}" = "true" ]; then
    case "${BACKUP_REMOTE}" in
        *:*) ;;
        *) configuration_failure "BACKUP_REMOTE must be a remote path such as encrypted:pos-production." ;;
    esac
    if [ -z "${RCLONE_CONFIG:-}" ] || [ -z "${RCLONE_CONFIG_PASS:-}" ] \
        || [ ! -r "${RCLONE_CONFIG}" ] \
        || ! rclone --ask-password=false config encryption check >/dev/null 2>&1; then
        configuration_failure "Off-site backups require a readable, encrypted rclone config and its password."
    fi
    remote_name=${BACKUP_REMOTE%%:*}
    remote_config=$(rclone --ask-password=false config show "${remote_name}" 2>/dev/null \
        || true)
    remote_type=$(printf '%s\n' "${remote_config}" \
        | awk -F ' = ' '$1 == "type" { print $2; exit }')
    filename_encryption=$(printf '%s\n' "${remote_config}" \
        | awk -F ' = ' '$1 == "filename_encryption" { print $2; exit }')
    directory_encryption=$(printf '%s\n' "${remote_config}" \
        | awk -F ' = ' '$1 == "directory_name_encryption" { print $2; exit }')
    underlying=$(printf '%s\n' "${remote_config}" \
        | awk -F ' = ' '$1 == "remote" { print $2; exit }')
    underlying_name=${underlying%%:*}
    underlying_config=$(rclone --ask-password=false config show "${underlying_name}" 2>/dev/null \
        || true)
    underlying_type=$(printf '%s\n' "${underlying_config}" \
        | awk -F ' = ' '$1 == "type" { print $2; exit }')
    if [ "${remote_type}" != "crypt" ] \
        || { [ -n "${filename_encryption}" ] && [ "${filename_encryption}" != "standard" ]; } \
        || { [ -n "${directory_encryption}" ] && [ "${directory_encryption}" != "true" ]; } \
        || [ -z "${underlying_type}" ] || [ "${underlying_type}" = "local" ]; then
        configuration_failure "BACKUP_REMOTE must be an rclone crypt remote with encrypted names over a configured remote provider."
    fi
fi
for retention_days in "${BACKUP_REMOTE_KEEP_DAILY_DAYS:-30}" "${BACKUP_REMOTE_KEEP_WEEKLY_DAYS:-365}"; do
    is_positive_integer "${retention_days}" \
        || configuration_failure "Remote backup retention must be a positive whole number of days no greater than 9999."
done
if [ "${BACKUP_ALERT_REQUIRED}" = "true" ]; then
    case "${BACKUP_ALERT_URL:-}" in
        https://*) ;;
        *) configuration_failure "Production backup alerts require an HTTPS BACKUP_ALERT_URL." ;;
    esac
fi

stamp=$(date +%Y%m%d-%H%M%S)
weekday=$(date +%u)

mkdir -p "${BACKUP_DIR}/daily" "${BACKUP_DIR}/weekly"

db_file="${BACKUP_DIR}/daily/pos-${stamp}.dump"
media_file="${BACKUP_DIR}/daily/media-${stamp}.tar.gz"
db_tmp="${db_file}.partial"
media_tmp="${media_file}.partial"

# A failed run must not leave a filename that restore.sh could mistake for the
# newest usable backup. Keep all work in .partial files until it has been read
# back successfully.
trap finish EXIT

echo "Dumping ${PGDATABASE} from ${PGHOST}..."
# --clean --if-exists so the dump can be restored over an existing database
# without a manual drop first, which is one fewer step to get wrong at the
# moment somebody is already having a bad day.
pg_dump --format=custom --clean --if-exists --file="${db_tmp}"

if [ ! -s "${db_tmp}" ]; then
    echo "Dump file is empty. Refusing to call this a backup." >&2
    exit 1
fi

# Read it back before believing it. pg_restore --list parses the archive's
# table of contents, so a truncated or corrupt dump fails here rather than
# during a restore six weeks from now.
if ! pg_restore --list "${db_tmp}" > /dev/null 2>&1; then
    echo "Dump is not readable by pg_restore. Refusing to keep it." >&2
    exit 1
fi

table_count=$(pg_restore --list "${db_tmp}" | grep -c "TABLE DATA" || true)
echo "Dump verified: ${db_tmp} ($(du -h "${db_tmp}" | cut -f1), ${table_count} tables with data)"

if [ -d "${MEDIA_DIR}" ]; then
    if ! tar -czf "${media_tmp}" -C "${MEDIA_DIR}" .; then
        echo "Media archive failed. Refusing to report a complete backup." >&2
        exit 1
    fi
    if [ ! -s "${media_tmp}" ] || ! tar -tzf "${media_tmp}" > /dev/null 2>&1; then
        echo "Media archive is empty or unreadable. Refusing to report a complete backup." >&2
        exit 1
    fi
    mv "${media_tmp}" "${media_file}"
    echo "Media verified: ${media_file} ($(du -h "${media_file}" | cut -f1))"
elif [ "${MEDIA_BACKUP_REQUIRED}" = "true" ]; then
    echo "Media directory ${MEDIA_DIR} is missing. Refusing to report a complete backup. Set MEDIA_BACKUP_REQUIRED=false only for a deployment that intentionally stores no media." >&2
    exit 1
elif [ "${MEDIA_BACKUP_REQUIRED}" = "false" ]; then
    echo "WARNING: media backup is explicitly disabled and ${MEDIA_DIR} is absent; this backup contains the database only." >&2
fi

# Publish the database dump only after both archive checks have succeeded.
mv "${db_tmp}" "${db_file}"
echo "Dump written: ${db_file}"

# Monday's copy is kept as the weekly. A hardlink rather than a second dump, so
# retention costs nothing until the daily is pruned.
if [ "${weekday}" = "1" ]; then
    ln -f "${db_file}" "${BACKUP_DIR}/weekly/pos-${stamp}.dump"
    [ -f "${media_file}" ] && ln -f "${media_file}" "${BACKUP_DIR}/weekly/media-${stamp}.tar.gz"
    echo "Kept as this week's weekly copy."
fi

if [ "${BACKUP_REMOTE_REQUIRED}" = "true" ]; then
    remote_dir="${BACKUP_REMOTE}/daily"
    # Retention runs against both trees after upload. On a fresh deployment
    # the weekly tree does not exist until Monday; creating both encrypted
    # directories here keeps the first non-Monday backup from failing during
    # pruning after its uploads have already succeeded.
    rclone mkdir "${BACKUP_REMOTE}/daily"
    rclone mkdir "${BACKUP_REMOTE}/weekly"
    echo "Uploading database archive to encrypted remote..."
    rclone copyto "${db_file}" "${remote_dir}/$(basename "${db_file}")"
    local_hash=$(sha256sum "${db_file}" | awk '{print $1}')
    remote_hash=$(rclone cat "${remote_dir}/$(basename "${db_file}")" | sha256sum | awk '{print $1}')
    [ "${local_hash}" = "${remote_hash}" ] || {
        echo "Remote database verification failed." >&2
        exit 1
    }

    if [ -f "${media_file}" ]; then
        rclone copyto "${media_file}" "${remote_dir}/$(basename "${media_file}")"
        local_hash=$(sha256sum "${media_file}" | awk '{print $1}')
        remote_hash=$(rclone cat "${remote_dir}/$(basename "${media_file}")" | sha256sum | awk '{print $1}')
        [ "${local_hash}" = "${remote_hash}" ] || {
            echo "Remote media verification failed." >&2
            exit 1
        }
    fi

    if [ "${weekday}" = "1" ]; then
        rclone copyto "${BACKUP_DIR}/weekly/pos-${stamp}.dump" \
            "${BACKUP_REMOTE}/weekly/pos-${stamp}.dump"
        local_hash=$(sha256sum "${BACKUP_DIR}/weekly/pos-${stamp}.dump" | awk '{print $1}')
        remote_hash=$(rclone cat "${BACKUP_REMOTE}/weekly/pos-${stamp}.dump" | sha256sum | awk '{print $1}')
        [ "${local_hash}" = "${remote_hash}" ] || {
            echo "Remote weekly database verification failed." >&2
            exit 1
        }
        if [ -f "${BACKUP_DIR}/weekly/media-${stamp}.tar.gz" ]; then
            rclone copyto "${BACKUP_DIR}/weekly/media-${stamp}.tar.gz" \
                "${BACKUP_REMOTE}/weekly/media-${stamp}.tar.gz"
            local_hash=$(sha256sum "${BACKUP_DIR}/weekly/media-${stamp}.tar.gz" | awk '{print $1}')
            remote_hash=$(rclone cat "${BACKUP_REMOTE}/weekly/media-${stamp}.tar.gz" | sha256sum | awk '{print $1}')
            [ "${local_hash}" = "${remote_hash}" ] || {
                echo "Remote weekly media verification failed." >&2
                exit 1
            }
        fi
    fi

    # Remote retention is longer than local retention. The encrypted remote
    # stores opaque names, so age-based pruning works without exposing dates.
    rclone delete --min-age "${BACKUP_REMOTE_KEEP_DAILY_DAYS:-30}d" \
        "${BACKUP_REMOTE}/daily"
    rclone delete --min-age "${BACKUP_REMOTE_KEEP_WEEKLY_DAYS:-365}d" \
        "${BACKUP_REMOTE}/weekly"
fi

prune() {
    directory="$1"
    pattern="$2"
    keep="$3"
    # These patterns are fixed by the caller and filenames are generated by
    # this script, so the intentional glob and mtime ordering are safe here.
    # shellcheck disable=SC2012,SC2086
    ls -1t "${directory}"/${pattern} 2>/dev/null | tail -n "+$((keep + 1))" | while read -r old; do
        echo "Pruning ${old}"
        rm -f "${old}"
    done
}

prune "${BACKUP_DIR}/daily" "pos-*.dump" "${KEEP_DAILY}"
prune "${BACKUP_DIR}/daily" "media-*.tar.gz" "${KEEP_DAILY}"
prune "${BACKUP_DIR}/weekly" "pos-*.dump" "${KEEP_WEEKLY}"
prune "${BACKUP_DIR}/weekly" "media-*.tar.gz" "${KEEP_WEEKLY}"

# The controlled timestamp filenames contain no whitespace or glob characters.
# shellcheck disable=SC2012
echo "Backup complete: $(ls -1 "${BACKUP_DIR}"/daily/pos-*.dump 2>/dev/null | wc -l) daily, $(ls -1 "${BACKUP_DIR}"/weekly/pos-*.dump 2>/dev/null | wc -l) weekly."
