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
