"""
Test settings.

The important property here is what is *not* changed: the database role stays
the same non-superuser, NOBYPASSRLS role used in development and production.
Running the isolation suite as a role that can bypass isolation would make the
tests pass without proving anything.
"""

from .base import *  # noqa: F403
from .base import REST_FRAMEWORK

DEBUG = False

# Fast and deterministic; the tests assert on authentication outcomes, not on
# how long a hash takes to compute.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Local memory rather than Redis, so the suite needs no second service running
# and one test's lockout counters cannot bleed into the next. The lockout code
# uses only the standard cache API, so it behaves identically on either backend.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pos-test",
    }
}

# Tenant status changes must be observable immediately in tests rather than
# after a cache window, so suspension tests assert behaviour and not timing.
TENANT_STATUS_CACHE_SECONDS = 0

# Throttles off by default: they count requests per address, and a test file
# making dozens of sign-in calls would trip them for reasons unrelated to what
# it is asserting. The tests that care about throttling turn them back on.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {"pin-login": None, "login": None}

# Tests drive requests directly, with no proxy in front.
TRUSTED_PROXY_HOPS = 0

# A fixed key so the encrypted-credential tests can round-trip. Fernet output is
# randomised per encryption, so a constant key here still proves nothing about
# ciphertext being predictable - it only lets the tests decrypt what they wrote.
DARAJA_ENCRYPTION_KEY = "43febadkU_EsOJwi_H2XjCeFZmuyri-njh-JIfqJMho="
MPESA_CALLBACK_BASE_URL = "https://pos.test"
