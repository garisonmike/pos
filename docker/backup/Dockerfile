# A small image that can talk to Postgres and run on a schedule.
#
# The postgres client version must match the server's major version: pg_dump
# refuses to dump from a newer server than it was built against, and finding
# that out during a restore is the worst possible time.
FROM postgres:18-alpine

RUN apk add --no-cache tzdata

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
