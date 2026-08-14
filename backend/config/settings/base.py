"""
Settings shared by every environment.

Environment-specific modules (dev, prod, test) import everything from here and
override only what genuinely differs, so there is one place to look when asking
"how is this configured".
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-development-key-change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# The Django admin is the platform control surface, so it is mounted somewhere
# unguessable rather than at the well-known /admin/ path. This is not a security
# boundary on its own (authentication and the platform-admin check are), it just
# keeps the control surface out of the way of routine scanning.
PLATFORM_ADMIN_URL = env("PLATFORM_ADMIN_URL", default="ops-console/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    # Local
    "apps.core",
    "apps.tenants",
    "apps.accounts",
    "apps.stores",
    "apps.catalog",
    "apps.inventory",
    "apps.sales",
    "apps.payments",
    "apps.sync",
    "apps.shifts",
    "apps.compliance",
    "apps.platform_admin",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Must sit after authentication so the Django admin session is available,
    # and it opens its own transaction so that `SET LOCAL app.tenant_id` is
    # scoped to the request. See apps/core/middleware.py for why that matters.
    "apps.core.middleware.TenantBindingMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="pos"),
        "USER": env("DB_USER", default="pos_app"),
        "PASSWORD": env("DB_PASSWORD", default="pos_app"),
        "HOST": env("DB_HOST", default="db"),
        "PORT": env.int("DB_PORT", default=5432),
        # Deliberately False. TenantBindingMiddleware opens the request
        # transaction itself so that the tenant binding is established inside
        # it; letting Django also wrap the view would only add a savepoint.
        "ATOMIC_REQUESTS": False,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# auth.E003 requires USERNAME_FIELD to be globally unique. It deliberately is
# not: usernames are unique within a business, enforced by a composite unique
# constraint on (tenant, username). Global uniqueness would mean the second
# shop to sign up finds its staff names already taken by strangers. The
# ambiguity the check guards against is handled in UserManager, whose
# get_by_natural_key resolves only platform administrators.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        # Closed by default: a view has to opt in to being public. The reverse
        # default means one forgotten permission class exposes tenant data.
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=env.int("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", default=60)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=env.int("JWT_REFRESH_TOKEN_LIFETIME_DAYS", default=14)
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "POS Platform API",
    "DESCRIPTION": (
        "Multi-tenant point of sale platform for Kenyan retail, restaurant, "
        "salon and pharmacy businesses.\n\n"
        "Every endpoint below the /api/v1/ prefix operates inside exactly one "
        "tenant, resolved from the access token. Endpoints under "
        "/api/v1/platform/ are the exception: they are cross-tenant and "
        "restricted to platform administrators."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    "ENUM_NAME_OVERRIDES": {
        "UserRoleEnum": "apps.accounts.models.UserRole.choices",
        "TenantStatusEnum": "apps.tenants.models.TenantStatus.choices",
        "BusinessTypeEnum": "apps.tenants.models.BusinessType.choices",
    },
}

# Seed credentials for the platform admin created on first boot.
PLATFORM_ADMIN_USERNAME = env("PLATFORM_ADMIN_USERNAME", default="platform")
PLATFORM_ADMIN_PASSWORD = env("PLATFORM_ADMIN_PASSWORD", default="")
PLATFORM_ADMIN_EMAIL = env("PLATFORM_ADMIN_EMAIL", default="admin@example.com")

# How long a tenant's active/suspended status is trusted before being re-read.
# Short enough that suspending a tenant takes effect quickly, long enough that
# a busy till is not making an extra query on every request.
TENANT_STATUS_CACHE_SECONDS = env.int("TENANT_STATUS_CACHE_SECONDS", default=60)

# Redis backs the tenant status cache and the till lockout counters. Both are
# process-shared state: with more than one API worker, a per-process cache
# would let each worker keep its own view of whether a business is suspended,
# and would give an attacker one full set of PIN attempts per worker.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://cache:6379/0"),
        "KEY_PREFIX": "pos",
    }
}

# How many proxies of ours sit in front of Django. Decides which
# X-Forwarded-For entry is believed when the M-Pesa callback allowlist checks
# where a request came from: with one trusted proxy the LAST entry is the one it
# appended, and everything to its left is whatever the caller sent.
#
# None rather than 0 on purpose - production refuses to start until it is set,
# because neither guess is safe. See apps/core/checks.py, pos.E004.
TRUSTED_PROXY_HOPS = env.int("TRUSTED_PROXY_HOPS", default=None)

# Encrypts each tenant's Daraja credentials at rest. Deliberately separate from
# SECRET_KEY: that key is rotated for its own reasons - a leak, a policy, a new
# deployment - and rotating it must not destroy every tenant's payment
# configuration. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DARAJA_ENCRYPTION_KEY = env("DARAJA_ENCRYPTION_KEY", default="")

# Where Safaricom should post STK results. Must be publicly reachable in a real
# deployment; the callback path itself carries the token that resolves a tenant.
MPESA_CALLBACK_BASE_URL = env("MPESA_CALLBACK_BASE_URL", default="http://localhost:8000")

# How long an STK prompt is considered live. Safaricom's own window is about a
# minute; a little longer here avoids declaring a payment lapsed while the
# customer is still typing their PIN.
MPESA_INTENT_TIMEOUT_SECONDS = env.int("MPESA_INTENT_TIMEOUT_SECONDS", default=90)

# Swapped for a fake in tests. A setting rather than an environment sniff, so
# a test can install one without pretending to be a different deployment.
DARAJA_CLIENT_FACTORY = None

# How long past a prompt's expiry to wait before asking Daraja what happened.
# On top of the prompt's own window, so a customer still entering their PIN is
# not chased with a question Daraja cannot yet answer.
MPESA_RECONCILE_GRACE_SECONDS = env.int("MPESA_RECONCILE_GRACE_SECONDS", default=120)

# Till PIN lockout. See apps.accounts.lockout for why this is a lockout rather
# than only a request-rate limit.
PIN_LOCKOUT_MAX_ATTEMPTS = env.int("PIN_LOCKOUT_MAX_ATTEMPTS", default=5)
PIN_LOCKOUT_SECONDS = env.int("PIN_LOCKOUT_SECONDS", default=900)

# Blunt outer limits on the sign-in routes, independent of the lockout above.
# The lockout stops one device being ground down; these stop a caller working
# through many devices or business slugs from one address.
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = (
    "rest_framework.throttling.ScopedRateThrottle",
)
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "pin-login": env("THROTTLE_PIN_LOGIN", default="20/min"),
    "login": env("THROTTLE_LOGIN", default="30/min"),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
