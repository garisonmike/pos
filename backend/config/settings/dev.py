"""Development settings: what `docker compose up` runs."""

from .base import *  # noqa: F403

DEBUG = True

# The Flutter client runs from a device or emulator during development, so the
# origin is unpredictable. Locked down properly in prod.py.
CORS_ALLOW_ALL_ORIGINS = True

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pos-dev",
    }
}
