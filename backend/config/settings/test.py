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

# ---------------------------------------------------------------------------
# Enough configuration to boot without anybody's .env
# ---------------------------------------------------------------------------
#
# The boot checks (pos.E001-E004) skip themselves when DEBUG is True. This
# module sets DEBUG False, so they run - and with the values published in
# .env.example they fail, which is exactly what they are for.
#
# That made the suite unrunnable under its own settings module, and the way
# that went unnoticed for six milestones is worth understanding: docker compose
# exports DJANGO_SETTINGS_MODULE=config.settings.dev, the environment variable
# beat the DJANGO_SETTINGS_MODULE in pyproject.toml, and every test ran under
# development settings instead. DEBUG was True there, so the checks stayed
# quiet. See the --ds flag in pyproject.toml's addopts, which is what now
# forces this module regardless of the environment.
#
# These two values exist so the suite depends on nothing outside the repository
# and are meaningless beyond it: no deployment reads this module.
SECRET_KEY = "test-suite-signing-key-not-used-outside-the-tests-9f3a2c7b1e"
PLATFORM_ADMIN_URL = "test-console-4d19b/"

# Fast and deterministic; the tests assert on authentication outcomes, not on
# how long a hash takes to compute.
#
# Worth keeping in view: this is not a small saving. With the real hasher in
# play - which is what happened while dev settings were in effect - the suite
# took 29 minutes. Under this module it takes two.
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
