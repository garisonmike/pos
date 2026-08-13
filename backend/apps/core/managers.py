"""Managers that scope querysets to the tenant bound to the current request."""

from __future__ import annotations

from django.db import models

from apps.core.tenancy import get_current_tenant_id


class TenantQuerySet(models.QuerySet):
    """QuerySet with an explicit escape hatch from tenant scoping."""

    def all_tenants(self) -> TenantQuerySet:
        """Drop the automatic tenant filter.

        Only meaningful on a platform surface, where Row-Level Security has
        been lifted. On a tenant-facing request the database will still refuse
        to return other tenants' rows, so this is safe to call by mistake - it
        just will not do what the caller hoped.
        """
        return self.model._base_manager.get_queryset()


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Default manager for tenant-owned models.

    This is the convenience layer, not the security layer. Row-Level Security
    is what makes cross-tenant access impossible; this manager exists so that
    ordinary code reads naturally (``Item.objects.all()`` means "this tenant's
    items") and so that a missing filter shows up as an obviously empty result
    during development rather than as a puzzling database error.

    When no tenant is bound the filter is skipped entirely. That sounds unsafe
    and is not: with no binding, the database policy matches nothing, so the
    caller gets an empty result anyway. The only context where such a query
    returns rows is a platform surface running under an explicit bypass, which
    is exactly where cross-tenant access is intended.
    """

    def get_queryset(self) -> TenantQuerySet:
        queryset = super().get_queryset()
        tenant_id = get_current_tenant_id()
        if tenant_id is not None:
            queryset = queryset.filter(tenant_id=tenant_id)
        return queryset
