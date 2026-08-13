"""
Startup checks for configuration that is dangerous to get wrong quietly.

Each of these guards a value that is harmless in development, is meant to be
changed before deployment, and gives no visible sign when it has not been. The
checks turn "works fine, quietly insecure" into "refuses to start".
"""

from __future__ import annotations

from django.test import override_settings

from apps.core.checks import (
    PLACEHOLDER_PLATFORM_ADMIN_URL,
    PLACEHOLDER_SECRET_KEY,
    check_cache_is_shared,
    check_platform_admin_url,
    check_secret_key,
)


class TestPlatformAdminUrl:
    @override_settings(DEBUG=False, PLATFORM_ADMIN_URL=PLACEHOLDER_PLATFORM_ADMIN_URL)
    def test_the_published_placeholder_is_refused(self):
        """The value in .env.example is public; deploying it is not acceptable."""
        errors = check_platform_admin_url(None)
        assert [error.id for error in errors] == ["pos.E001"]

    @override_settings(DEBUG=False, PLATFORM_ADMIN_URL=PLACEHOLDER_PLATFORM_ADMIN_URL)
    def test_the_error_says_what_to_do(self):
        """A check that stops a deployment must explain how to get past it."""
        hint = check_platform_admin_url(None)[0].hint
        assert "PLATFORM_ADMIN_URL" in hint
        assert ".env" in hint

    @override_settings(DEBUG=False, PLATFORM_ADMIN_URL="admin/")
    def test_the_well_known_admin_path_is_refused(self):
        assert [error.id for error in check_platform_admin_url(None)] == ["pos.E002"]

    @override_settings(DEBUG=False, PLATFORM_ADMIN_URL="")
    def test_an_empty_path_is_refused(self):
        """Empty would mount the console at the site root."""
        assert [error.id for error in check_platform_admin_url(None)] == ["pos.E002"]

    @override_settings(DEBUG=False, PLATFORM_ADMIN_URL="console-b3f19a77/")
    def test_a_changed_value_passes(self):
        assert check_platform_admin_url(None) == []

    @override_settings(DEBUG=True, PLATFORM_ADMIN_URL=PLACEHOLDER_PLATFORM_ADMIN_URL)
    def test_development_is_left_alone(self):
        """The placeholder is the point in development.

        Making it fail locally would mean every contributor has to invent a
        value before the project runs at all, which is friction with no
        security benefit on a laptop.
        """
        assert check_platform_admin_url(None) == []


class TestSecretKey:
    @override_settings(DEBUG=False, SECRET_KEY=PLACEHOLDER_SECRET_KEY)
    def test_the_development_key_is_refused_in_production(self):
        """This key signs every access token on the platform.

        Published, it would let anyone mint a token for any business, which
        makes it strictly worse than the console path being guessable.
        """
        assert [error.id for error in check_secret_key(None)] == ["pos.E003"]

    @override_settings(DEBUG=False, SECRET_KEY="a-real-and-sufficiently-long-secret-key")
    def test_a_real_key_passes(self):
        assert check_secret_key(None) == []

    @override_settings(DEBUG=True, SECRET_KEY=PLACEHOLDER_SECRET_KEY)
    def test_development_is_left_alone(self):
        assert check_secret_key(None) == []


class TestSharedCache:
    @override_settings(
        DEBUG=False,
        CACHES={
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        },
    )
    def test_a_per_process_cache_is_warned_about(self):
        """Lockout counters in a per-process cache are weaker than they look.

        With several workers, each keeps its own count, so an attacker gets the
        full allowance once per worker.
        """
        warnings = check_cache_is_shared(None)
        assert [warning.id for warning in warnings] == ["pos.W001"]

    @override_settings(
        DEBUG=False,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": "redis://cache:6379/0",
            }
        },
    )
    def test_a_shared_cache_passes(self):
        assert check_cache_is_shared(None) == []

    @override_settings(
        DEBUG=True,
        CACHES={
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        },
    )
    def test_development_is_left_alone(self):
        assert check_cache_is_shared(None) == []


def test_the_checks_are_registered_with_django():
    """Registration is what makes these run before a command, not the code above.

    Without it every assertion in this file would still pass while the checks
    never actually ran.
    """
    from django.core.checks import registry

    registered = {check.__name__ for check in registry.registry.get_checks()}
    assert {
        "check_platform_admin_url",
        "check_secret_key",
        "check_cache_is_shared",
    } <= registered
