"""Branch endpoints."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets

from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.permissions import ReadOnlyOrManager
from apps.stores.models import Store
from apps.stores.serializers import StoreSerializer


@extend_schema(tags=["stores"])
class StoreViewSet(viewsets.ModelViewSet):
    """Branches belonging to the signed-in user's business.

    Cashiers read this so a till can name the branch it is selling from.
    Only managers and owners create or change branches.

    There is no delete: a branch with sales history behind it cannot be removed
    without orphaning those records, so branches are deactivated instead.
    """

    serializer_class = StoreSerializer
    permission_classes = [ReadOnlyOrManager]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        """Branches of the caller's business."""
        # drf-spectacular introspects this without a real request, so
        # request.user is anonymous and has no tenant. Returning an empty
        # queryset lets it derive the model - and therefore correct path
        # parameter types - without weakening anything at runtime.
        if getattr(self, "swagger_fake_view", False):
            return Store.objects.none()
        return Store.objects.filter(tenant=self.request.user.tenant).order_by("name")

    def perform_create(self, serializer):
        """Attach the caller's business and record the change.

        The tenant comes from the authenticated user rather than the payload,
        so a client cannot create a branch inside a business it does not
        belong to.
        """
        store = serializer.save(tenant=self.request.user.tenant)
        record_audit(
            action=AuditAction.CREATE,
            entity=store,
            actor=self.request.user,
            request=self.request,
            after={"name": store.name, "code": store.code},
        )

    def perform_update(self, serializer):
        before = {
            "name": serializer.instance.name,
            "code": serializer.instance.code,
            "is_active": serializer.instance.is_active,
        }
        store = serializer.save()
        record_audit(
            action=AuditAction.UPDATE,
            entity=store,
            actor=self.request.user,
            request=self.request,
            before=before,
            after={"name": store.name, "code": store.code, "is_active": store.is_active},
        )
