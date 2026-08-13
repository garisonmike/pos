"""Development settings: what `docker compose up` runs."""

from .base import *  # noqa: F403

DEBUG = True

# The Flutter client runs from a device or emulator during development, so the
# origin is unpredictable. Locked down properly in prod.py.
CORS_ALLOW_ALL_ORIGINS = True

# Cache comes from base: the Redis service in the compose file. Development
# deliberately uses the same backend as production, because the till lockout
# depends on counters being shared across processes and a local-memory cache
# would hide that difference until it mattered.
