"""
Test settings.

The important property here is what is *not* changed: the database role stays
the same non-superuser, NOBYPASSRLS role used in development and production.
Running the isolation suite as a role that can bypass isolation would make the
tests pass without proving anything.
"""

from .base import *  # noqa: F403

DEBUG = False

# Fast and deterministic; the tests assert on authentication outcomes, not on
# how long a hash takes to compute.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pos-test",
    }
}

# Tenant status changes must be observable immediately in tests rather than
# after a cache window, so suspension tests assert behaviour and not timing.
TENANT_STATUS_CACHE_SECONDS = 0
