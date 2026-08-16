"""
Settings for a deployed instance.

Distinct from ``prod.py``, which was a placeholder kept honest while the
milestones landed. This is the module a real deployment runs, and it is
deliberately strict: several things that are warnings in development are hard
failures here, because the failure mode of a misconfigured production instance
is a shop trading on a system that is quietly not isolating its tenants.

Everything that varies between deployments comes from the environment. Nothing
in this file is a secret, and nothing in it should ever become one.
"""

from __future__ import annotations

from .base import *  # noqa: F403
from .base import BASE_DIR, env

DEBUG = False

# ---------------------------------------------------------------------------
# Who may address this instance
# ---------------------------------------------------------------------------
#
# No default. A deployment that has not said which hostnames it answers to is
# one that will happily serve a Host header somebody else chose, and Django's
# own default of an empty list at least fails loudly rather than serving.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
CORS_ALLOWED_ORIGINS = env.list("DJANGO_CORS_ALLOWED_ORIGINS", default=[])

# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------
#
# Caddy terminates TLS and proxies over the compose network in plain HTTP, so
# Django learns the original scheme from this header. Trusting it is only safe
# because nothing but Caddy can reach gunicorn - see TRUSTED_PROXY_HOPS below,
# and the topology note in DEPLOYMENT.md.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Off by default here, and that is deliberate: Caddy already redirects HTTP to
# HTTPS before a request reaches Django, so a second redirect inside the app is
# redundant, and it breaks the container healthcheck, which speaks plain HTTP
# over the internal network and has no scheme header to offer.
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=False)

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True

# A year, with subdomains and preload. Worth understanding before switching on:
# HSTS is a promise a browser remembers, so serving this domain over plain HTTP
# afterwards stops working for anybody who has visited once.
SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=31_536_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# ---------------------------------------------------------------------------
# The proxy in front
# ---------------------------------------------------------------------------
#
# **One hop, and the deployment is what makes that true.**
#
# The M-Pesa callback reads the client IP from the *last* entry of
# X-Forwarded-For, because the leftmost entries are supplied by the caller and
# anybody can write anything there. The last entry is the one our own proxy
# appended, and it is only the real peer if exactly one trusted proxy stands
# between the internet and Django.
#
# That is enforced by topology rather than assumed:
#
#   * Caddy is the only service that binds a host port.
#   * gunicorn, Postgres and Redis are reachable on the compose network only.
#   * Caddy appends exactly one entry to X-Forwarded-For.
#
# Adding a CDN or a cloud load balancer in front makes this 2. DEPLOYMENT.md
# carries a forged-header check that proves the value against the deployment
# rather than against anybody's reasoning about it - run it after any change to
# what sits in front.
#
# There is no default: pos.E004 refuses to start without it, which turns
# "forgot" into a crash rather than a spoofable allowlist.
TRUSTED_PROXY_HOPS = env.int("TRUSTED_PROXY_HOPS")

# ---------------------------------------------------------------------------
# Static and media
# ---------------------------------------------------------------------------
#
# Both are served by Caddy straight off a volume, never through Django. Static
# is small - the admin and DRF's browsable API - because the till is a Flutter
# app and asks for JSON. Media is tenant logos, which appear on receipts.
STATIC_ROOT = env.str("DJANGO_STATIC_ROOT", default=str(BASE_DIR / "staticfiles"))
MEDIA_ROOT = env.str("DJANGO_MEDIA_ROOT", default=str(BASE_DIR / "media"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Django's own manifest storage, not WhiteNoise: Caddy serves these files
    # off the volume, so nothing needs to serve them from inside Python. What
    # is wanted is the hashed filenames, so Caddy can cache hard without a
    # deploy leaving a browser holding last month's CSS.
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
#
# To stdout, for the container runtime to collect. A file inside a container is
# a log nobody reads and a disk nobody watches.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env.str("DJANGO_LOG_LEVEL", default="INFO")},
    "loggers": {
        # Django logs a warning for every 4xx. A till on a flaky connection
        # produces those constantly, and at INFO they drown everything worth
        # reading.
        "django.request": {
            "handlers": ["console"],
            "level": env.str("DJANGO_REQUEST_LOG_LEVEL", default="ERROR"),
            "propagate": False,
        },
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
