"""
The middleware that decides, for every request, which tenant's data is visible.

Two modes, chosen by URL prefix and nothing else:

**Tenant mode** (everything under ``/api/v1/`` except the platform prefix)
    The tenant is read from the ``tenant_id`` claim on the access token and
    bound for the life of the request. No token, or an unreadable one, means no
    binding - and with no binding the Row-Level Security policies match nothing,
    so the request sees an empty database rather than someone else's shop.

**Platform mode** (the Django admin and ``/api/v1/platform/``)
    Isolation is lifted, because provisioning a business and billing across all
    of them are genuinely cross-tenant jobs. Every view behind these two
    prefixes independently requires the caller to be a platform administrator;
    see ``apps.core.permissions.IsPlatformAdmin`` and ``PlatformAdminSite``.

Deciding by URL prefix rather than by inspecting the authenticated user avoids
a bootstrap problem that would otherwise be circular: the user table is itself
tenant-isolated, so working out whether the caller may bypass isolation would
require a query that isolation blocks. Prefixes are known before any query
runs, which breaks the cycle and, usefully, makes the set of routes where
isolation is lifted something a reviewer can enumerate by reading the URL conf.

The whole request runs inside one transaction because the tenant binding is set
with ``set_config(..., is_local => true)``, which is scoped to a transaction.
That scoping is the point: it guarantees the binding cannot survive into the
next request to reuse the same pooled connection.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse

from apps.core.tenancy import bypass_rls, tenant_context

logger = logging.getLogger(__name__)

#: Cache key prefix for the per-tenant status lookup.
_STATUS_CACHE_PREFIX = "tenant-status:"


class TenantBindingMiddleware:
    """Binds the request's tenant and refuses requests from inactive tenants."""

    def __init__(self, get_response):
        self.get_response = get_response
        admin_prefix = "/" + settings.PLATFORM_ADMIN_URL.lstrip("/")
        self.platform_prefixes = (admin_prefix, "/api/v1/platform/")

    def __call__(self, request):
        if self._is_platform_path(request.path):
            with transaction.atomic(), bypass_rls():
                return self.get_response(request)

        tenant_id = self._tenant_id_from_token(request)

        if tenant_id is not None:
            refusal = self._refuse_if_inactive(tenant_id)
            if refusal is not None:
                return refusal

        with transaction.atomic(), tenant_context(tenant_id):
            return self.get_response(request)

    def _is_platform_path(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.platform_prefixes)

    def _tenant_id_from_token(self, request) -> uuid.UUID | None:
        """Read the tenant claim off the bearer token, without touching the database.

        The token's signature and expiry are verified here, but nothing more.
        Authentication proper still happens in DRF; this only needs enough to
        decide which rows the request may see, and doing it without a query
        keeps the isolation decision independent of the tables it protects.
        """
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None

        raw_token = header.removeprefix("Bearer ").strip()
        if not raw_token:
            return None

        # Imported lazily: this module is constructed during startup, before
        # the app registry that simplejwt depends on is fully populated.
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken

        try:
            token = AccessToken(raw_token)
        except TokenError:
            # Leave it unbound and let DRF produce a proper 401 with a body the
            # client can act on. Raising here would return an opaque 500.
            return None

        claim = token.payload.get("tenant_id")
        if not claim:
            return None
        try:
            return uuid.UUID(str(claim))
        except ValueError:
            logger.warning("Discarding malformed tenant_id claim on access token")
            return None

    def _refuse_if_inactive(self, tenant_id: uuid.UUID) -> JsonResponse | None:
        """Block tills belonging to a suspended or cancelled business.

        Cached briefly so a busy till is not making an extra query per request,
        but not so long that suspending a non-paying customer takes effect only
        when their tokens happen to expire. The trade is bounded and explicit:
        at most ``TENANT_STATUS_CACHE_SECONDS`` of continued access.

        ``402 Payment Required`` is used rather than 403 so the Flutter client
        can tell "your subscription has lapsed, contact your provider" apart
        from "you are not allowed to do that", and show the right screen.
        """
        from apps.tenants.models import Tenant, TenantStatus

        cache_key = f"{_STATUS_CACHE_PREFIX}{tenant_id}"
        status = cache.get(cache_key)

        if status is None:
            status = (
                Tenant.objects.filter(pk=tenant_id)
                .values_list("status", flat=True)
                .first()
            ) or "MISSING"
            timeout = getattr(settings, "TENANT_STATUS_CACHE_SECONDS", 60)
            if timeout:
                cache.set(cache_key, status, timeout)

        if status in (TenantStatus.ACTIVE, TenantStatus.TRIAL):
            return None

        if status == "MISSING":
            return JsonResponse(
                {
                    "detail": "This business no longer exists.",
                    "code": "tenant_not_found",
                },
                status=401,
            )

        return JsonResponse(
            {
                "detail": (
                    "This business account is not active. Contact your "
                    "provider to restore access."
                ),
                "code": "tenant_inactive",
                "status": status,
            },
            status=402,
        )


def clear_tenant_status_cache(tenant_id) -> None:
    """Drop a cached status so suspension takes effect immediately.

    Called by the platform admin surface when it changes a tenant's status, so
    the operator sees the effect straight away instead of waiting out the cache
    window they configured for everyone else's benefit.
    """
    cache.delete(f"{_STATUS_CACHE_PREFIX}{tenant_id}")
