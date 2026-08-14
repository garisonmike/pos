"""
Startup checks for configuration that is dangerous to get wrong quietly.

These use Django's own check framework, which runs before ``runserver``,
``migrate`` and every other management command. A check registered at ``Error``
level stops the command outright, so "refuse to start" needs no custom
bootstrap code - and, usefully, it also fails a deployment's ``migrate`` step
rather than only the web process, so a bad configuration is caught before it
serves a single request.

The checks here all guard the same class of mistake: a value that is harmless
in development, is meant to be changed before deployment, and gives no visible
sign when it has not been.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register

#: The value shipped in .env.example. Kept here as a constant rather than read
#: from that file, so the check does not depend on a file that is absent in a
#: real deployment.
PLACEHOLDER_PLATFORM_ADMIN_URL = "ops-console-8f31c2/"

#: Development defaults for anything else that must not reach production.
PLACEHOLDER_SECRET_KEY = "insecure-development-key-change-me"


@register(deploy=False)
def check_platform_admin_url(app_configs, **kwargs) -> list:
    """Refuse to run with the published platform console path.

    The path is not a security boundary - authentication and the platform-admin
    check are - but it is published in this repository's ``.env.example``, so
    leaving it unchanged means the console sits at a location anyone reading the
    repository already knows.

    The placeholder deliberately stays in ``.env.example`` as documentation of
    the expected shape. This check is what makes forgetting to change it fail
    loudly instead of silently.
    """
    if settings.DEBUG:
        return []

    configured = getattr(settings, "PLATFORM_ADMIN_URL", "")

    if configured == PLACEHOLDER_PLATFORM_ADMIN_URL:
        return [
            Error(
                "PLATFORM_ADMIN_URL is still the placeholder published in "
                ".env.example.",
                hint=(
                    "Set PLATFORM_ADMIN_URL in your .env to something only you "
                    "know, for example 'console-<random>/'. Keep the trailing "
                    "slash. The placeholder stays in .env.example on purpose; "
                    "it is documentation, not a default to deploy."
                ),
                id="pos.E001",
            )
        ]

    if configured in ("admin/", "admin", ""):
        return [
            Error(
                f"PLATFORM_ADMIN_URL is set to {configured!r}, which is either "
                "empty or the well-known Django admin path.",
                hint=(
                    "The platform console controls every business on this "
                    "deployment. Mount it somewhere that is not the first path "
                    "a scanner tries."
                ),
                id="pos.E002",
            )
        ]

    return []


@register(deploy=False)
def check_secret_key(app_configs, **kwargs) -> list:
    """Refuse to run in production with the development signing key.

    The same class of mistake as above, and worse in consequence: this key signs
    every access token on the platform, so the published value would let anyone
    mint a token for any business.
    """
    if settings.DEBUG:
        return []

    if settings.SECRET_KEY == PLACEHOLDER_SECRET_KEY:
        return [
            Error(
                "DJANGO_SECRET_KEY is still the development placeholder.",
                hint=(
                    "This key signs every access token issued by the platform. "
                    "Generate one with: python -c \"import secrets; "
                    "print(secrets.token_urlsafe(64))\""
                ),
                id="pos.E003",
            )
        ]
    return []


@register(deploy=False)
def check_trusted_proxy_hops(app_configs, **kwargs) -> list:
    """Refuse to run in production without an explicit trusted-hop count.

    The M-Pesa callback allowlist decides whether real money is credited by
    reading a client address out of ``X-Forwarded-For``. Which entry of that
    header to believe depends entirely on how many proxies of ours sit in
    front - and there is no safe default to fall back on:

    * Guess too few and we read an entry the caller supplied, so anyone can
      claim to be Safaricom and have a forged callback credited.
    * Guess too many and we read nothing, so every genuine callback is refused.

    Zero is correct only when Django is exposed directly, which is not how this
    deploys. So production must say, rather than have the software assume.
    """
    if settings.DEBUG:
        return []

    if getattr(settings, "TRUSTED_PROXY_HOPS", None) is None:
        return [
            Error(
                "TRUSTED_PROXY_HOPS is not set.",
                hint=(
                    "Set it to the number of proxies of yours in front of "
                    "Django - 1 for a single nginx, Caddy or load balancer "
                    "terminating TLS, which is the usual shape. It decides "
                    "which X-Forwarded-For entry the M-Pesa callback allowlist "
                    "believes, so a wrong value either lets a forged callback "
                    "through or refuses every real one."
                ),
                id="pos.E004",
            )
        ]
    return []


@register(deploy=False)
def check_cache_is_shared(app_configs, **kwargs) -> list:
    """Warn when the cache is per-process in a deployment.

    Till lockout counts failed PIN attempts in the cache. With a local-memory
    cache and more than one worker, each worker keeps its own count, so an
    attacker gets the full allowance once per worker and the lockout is weaker
    than it appears. Tenant suspension has the same problem in milder form.

    A warning rather than an error, because a single-worker deployment is not
    actually broken by this - just fragile the moment it is scaled.
    """
    if settings.DEBUG:
        return []

    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if "locmem" in backend:
        return [
            Warning(
                "The default cache is local-memory, which is not shared between "
                "worker processes.",
                hint=(
                    "Till lockout counters and tenant suspension status live in "
                    "the cache. Point REDIS_URL at a Redis instance so every "
                    "worker sees the same counts."
                ),
                id="pos.W001",
            )
        ]
    return []
