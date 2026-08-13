"""
The platform operator's API.

Everything under this prefix runs with tenant isolation lifted, because
onboarding a business and billing across all of them are genuinely cross-tenant
jobs (see ``apps.core.middleware``). That makes these the most sensitive routes
in the system, so every one of them declares ``IsPlatformAdmin`` explicitly
rather than relying on a default, and a test asserts that no route here is
reachable by a tenant's own users.

Sign-in is the single exception: it has to be reachable before the caller has a
token. It reads only accounts with no tenant, so it cannot be used to probe a
business's staff list.
"""

from __future__ import annotations

from django.db.models import Count, Max, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import PlatformLoginSerializer, UserSerializer
from apps.accounts.tokens import issue_tokens_for
from apps.core.audit import record_audit
from apps.core.middleware import clear_tenant_status_cache
from apps.core.models import AuditAction
from apps.core.permissions import IsPlatformAdmin
from apps.platform_admin.serializers import (
    PlatformTenantSerializer,
    TenantProvisionSerializer,
    TenantStatusChangeSerializer,
    TenantUsageSerializer,
)
from apps.tenants.models import Tenant, TenantStatus
from apps.tenants.services import provision_tenant


@extend_schema(tags=["platform"])
class PlatformLoginView(APIView):
    """Sign in as the platform operator."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = PlatformLoginSerializer

    @extend_schema(
        summary="Platform operator sign-in",
        description=(
            "Signs in an account that belongs to no business. Tenant staff "
            "cannot sign in here, and platform operators cannot sign in at the "
            "tenant endpoint, so the two surfaces never overlap."
        ),
        request=PlatformLoginSerializer,
        responses={200: OpenApiResponse(description="Access and refresh tokens")},
    )
    def post(self, request):
        serializer = PlatformLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        user.last_login = timezone.now()
        user.save(update_fields=["last_login", "updated_at"])

        record_audit(
            action=AuditAction.LOGIN,
            entity=user,
            actor=user,
            request=request,
            tenant_id=None,
            after={"method": "platform"},
        )
        return Response({**issue_tokens_for(user), "user": UserSerializer(user).data})


@extend_schema(tags=["platform"])
class PlatformTenantViewSet(viewsets.ModelViewSet):
    """Onboard, inspect, suspend and reactivate businesses."""

    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformTenantSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = ["status", "business_type"]
    search_fields = ["name", "slug"]

    def get_queryset(self):
        """Every business on the platform, with a staff count attached."""
        return Tenant.objects.annotate(user_count=Count("users")).order_by("name")

    @extend_schema(
        summary="List businesses",
        responses={200: PlatformTenantSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Onboard a business",
        description=(
            "Creates a business, switches on the modules its type implies, and "
            "creates the owner's account. The owner then runs the setup wizard "
            "from the till to add their branch, tax rate and staff."
        ),
        request=TenantProvisionSerializer,
        responses={201: PlatformTenantSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = TenantProvisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant, owner = provision_tenant(
            name=data["name"],
            slug=data.get("slug", ""),
            business_type=data["business_type"],
            status=data["status"],
            trial_days=data["trial_days"],
            owner_username=data["owner_username"],
            owner_full_name=data["owner_full_name"],
            owner_password=data["owner_password"],
            owner_phone=data.get("owner_phone", ""),
            owner_email=data.get("owner_email", ""),
            actor=request.user,
        )

        body = PlatformTenantSerializer(tenant).data
        body["owner"] = {
            "id": str(owner.id),
            "username": owner.username,
            "full_name": owner.full_name,
        }
        return Response(body, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Suspend a business",
        description=(
            "Stops tills belonging to this business from trading. Takes effect "
            "within the tenant status cache window, without waiting for tokens "
            "to expire.\n\n"
            "Nothing is deleted. Sales already recorded stay, and a device that "
            "was offline during the suspension can still sync the sales it "
            "completed, because that money was already taken."
        ),
        request=TenantStatusChangeSerializer,
        responses={200: PlatformTenantSerializer},
    )
    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        return self._change_status(
            request, TenantStatus.SUSPENDED, AuditAction.SUSPEND
        )

    @extend_schema(
        summary="Reactivate a business",
        request=TenantStatusChangeSerializer,
        responses={200: PlatformTenantSerializer},
    )
    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        return self._change_status(
            request, TenantStatus.ACTIVE, AuditAction.REACTIVATE
        )

    def _change_status(self, request, new_status: str, audit_action: str) -> Response:
        """Shared implementation for suspend and reactivate.

        The cached status is cleared immediately so the operator sees the
        effect at once rather than waiting out a window that exists for the
        benefit of busy tills.
        """
        tenant = self.get_object()
        serializer = TenantStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        previous = tenant.status
        tenant.status = new_status
        tenant.save(update_fields=["status", "updated_at"])
        clear_tenant_status_cache(tenant.id)

        record_audit(
            action=audit_action,
            entity=tenant,
            actor=request.user,
            request=request,
            tenant_id=None,
            reason=serializer.validated_data.get("reason", ""),
            before={"status": previous},
            after={"status": new_status},
        )
        return Response(PlatformTenantSerializer(tenant).data)


@extend_schema(tags=["platform"])
class PlatformUsageView(APIView):
    """Per-business usage, for the operator's own invoicing.

    Deliberately counts rather than money: what a business is charged is
    settled outside this system, and putting subscription billing inside the
    product would tie a shop's ability to trade to a payment integration that
    can fail independently.
    """

    permission_classes = [IsPlatformAdmin]
    serializer_class = TenantUsageSerializer

    @extend_schema(
        summary="Usage per business",
        description=(
            "Counts of staff, branches, tills and catalogue items per business, "
            "with the enabled modules and the most recent sign-in. This is the "
            "view to read when working out what to invoice."
        ),
        responses={200: TenantUsageSerializer(many=True)},
    )
    def get(self, request):
        tenants = (
            Tenant.objects.annotate(
                user_count=Count("users", distinct=True),
                active_user_count=Count(
                    "users", filter=Q(users__is_active=True), distinct=True
                ),
                store_count=Count("stores_store_set", distinct=True),
                device_count=Count("accounts_device_set", distinct=True),
                item_count=Count("catalog_item_set", distinct=True),
                last_activity_at=Max("users__last_login"),
            )
            .prefetch_related("modules")
            .order_by("name")
        )

        payload = [
            {
                "tenant_id": tenant.id,
                "name": tenant.name,
                "slug": tenant.slug,
                "business_type": tenant.business_type,
                "status": tenant.status,
                "is_setup_complete": tenant.is_setup_complete,
                "user_count": tenant.user_count,
                "active_user_count": tenant.active_user_count,
                "store_count": tenant.store_count,
                "device_count": tenant.device_count,
                "item_count": tenant.item_count,
                "enabled_modules": [
                    module.module_key for module in tenant.modules.all() if module.is_enabled
                ],
                "last_activity_at": tenant.last_activity_at,
                "created_at": tenant.created_at,
            }
            for tenant in tenants
        ]
        return Response(TenantUsageSerializer(payload, many=True).data)


@extend_schema(tags=["platform"])
class PlatformTenantUsersView(APIView):
    """Staff of one business, for support purposes.

    Exists so the operator can help a shop that has locked itself out. It is
    read-only: resetting a password from here would create a way into a
    customer's data that leaves no trace in their own audit trail.
    """

    permission_classes = [IsPlatformAdmin]
    serializer_class = UserSerializer

    @extend_schema(
        summary="Staff of one business",
        responses={200: UserSerializer(many=True)},
    )
    def get(self, request, tenant_id):
        users = User.all_objects.filter(tenant_id=tenant_id).order_by("full_name")
        return Response(UserSerializer(users, many=True).data)
