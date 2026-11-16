#!/bin/sh
# Runs the nightly backup on a simple loop rather than through cron.
#
# A loop in the foreground is visible to `docker compose ps` and its output
# goes to the container log, where the rest of this system's output already
# goes. A cron daemon inside a container writes to a file nobody reads and
# fails silently, which is the specific failure this whole piece exists to
# avoid.

set -e

# A command passed in runs instead of the schedule loop. Without this, the
# entrypoint ignored its arguments and looped forever, so
# `docker compose run backup /scripts/backup.sh` hung rather than taking a
# backup - which is exactly how somebody would first try to run one by hand.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

BACKUP_HOUR="${BACKUP_HOUR:-2}"
BACKUP_REMOTE_REQUIRED="${BACKUP_REMOTE_REQUIRED:-false}"
BACKUP_ALERT_REQUIRED="${BACKUP_ALERT_REQUIRED:-false}"

fail_startup() {
    echo "$1" >&2
    case "${BACKUP_ALERT_URL:-}" in
        https://*)
            curl --fail --silent --show-error --max-time 15 \
                --request POST --header 'Content-Type: text/plain' \
                --data-binary "POS backup service failed to start; inspect backup logs." \
                "${BACKUP_ALERT_URL}" >/dev/null \
                || echo "WARNING: startup failure alert could not be delivered." >&2
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

for setting in "${BACKUP_REMOTE_REQUIRED}" "${BACKUP_ALERT_REQUIRED}"; do
    case "${setting}" in
        true|false) ;;
        *) fail_startup "Backup requirement flags must be true or false." ;;
    esac
done

case "${BACKUP_HOUR}" in
    [0-9]|1[0-9]|2[0-3]) ;;
    *) fail_startup "BACKUP_HOUR must be an hour from 0 through 23." ;;
esac

for retention in "${BACKUP_KEEP_DAILY:-7}" "${BACKUP_KEEP_WEEKLY:-4}"; do
    is_positive_integer "${retention}" \
        || fail_startup "Local backup retention must be a positive whole number no greater than 9999."
done

if [ "${BACKUP_REMOTE_REQUIRED}" = "true" ]; then
    case "${BACKUP_REMOTE:-}" in
        *:*) ;;
        *) fail_startup "BACKUP_REMOTE must be a remote path such as encrypted:pos-production." ;;
    esac
    if [ -z "${RCLONE_CONFIG:-}" ] \
        || [ -z "${RCLONE_CONFIG_PASS:-}" ] || [ ! -r "${RCLONE_CONFIG}" ] \
        || ! rclone --ask-password=false config encryption check >/dev/null 2>&1; then
        fail_startup "Off-site backups require BACKUP_REMOTE, an encrypted rclone config and its password."
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
        fail_startup "BACKUP_REMOTE must be a configured rclone crypt remote with file and directory name encryption."
    fi
fi

if [ "${BACKUP_ALERT_REQUIRED}" = "true" ]; then
    case "${BACKUP_ALERT_URL:-}" in
        https://*) ;;
        *) fail_startup "Production backup alerts require an HTTPS BACKUP_ALERT_URL." ;;
    esac
fi

echo "Backup service started. Nightly run at ${BACKUP_HOUR}:00 (container time: $(date))."

while true; do
    now_hour=$(date +%-H)
    now_min=$(date +%-M)

    if [ "$now_hour" -eq "$BACKUP_HOUR" ] && [ "$now_min" -lt 10 ]; then
        echo "--- Backup run starting at $(date) ---"
        if /scripts/backup.sh; then
            echo "--- Backup run finished at $(date) ---"
        else
            # Loud, and on stdout, so whatever is watching container logs sees
            # it. A backup that fails quietly is worse than no backup, because
            # somebody believes it is happening.
            echo "!!! BACKUP FAILED at $(date) - exit $? !!!" >&2
        fi
        # Past the window before checking again.
        sleep 3600
    fi

    sleep 300
done
