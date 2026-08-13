"""
A business's view of itself.

Every endpoint here operates on exactly one tenant: the caller's own, resolved
from their access token. None of them take a tenant identifier, so there is no
parameter an attacker could change to reach another business.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.permissions import IsOwner, IsTenantUser
from apps.tenants.models import TenantModule
from apps.tenants.serializers import (
    BusinessTemplateSerializer,
    TenantModuleSerializer,
    TenantSerializer,
    TenantSetupSerializer,
)
from apps.tenants.services import SetupAlreadyCompleted, complete_setup


@extend_schema(tags=["tenant"])
class TenantSettingsView(APIView):
    """Read and update the signed-in user's own business settings."""

    serializer_class = TenantSerializer

    def get_permissions(self):
        """Everyone reads; only the owner changes business-wide settings.

        A cashier's till needs the currency, VAT mode and receipt branding to
        render a sale correctly, so reading is open to all staff. Changing any
        of it affects every receipt the business issues, so it is the owner's.
        """
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsTenantUser()]
        return [IsOwner()]

    @extend_schema(
        summary="Own business settings",
        description=(
            "Returns the caller's business, including which modules are "
            "enabled. The till reads this at sign-in to decide what to show."
        ),
        responses={200: TenantSerializer},
    )
    def get(self, request):
        return Response(TenantSerializer(request.user.tenant).data)

    @extend_schema(
        summary="Update own business settings",
        description=(
            "Changes branding, contact details, tax PIN and VAT mode. Status "
            "and slug are not editable here; both are the platform operator's."
        ),
        request=TenantSerializer,
        responses={200: TenantSerializer},
    )
    def patch(self, request):
        tenant = request.user.tenant
        serializer = TenantSerializer(tenant, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        before = {
            "name": tenant.name,
            "vat_mode": tenant.vat_mode,
            "kra_pin": tenant.kra_pin,
            "receipt_header": tenant.receipt_header,
            "receipt_footer": tenant.receipt_footer,
        }
        tenant = serializer.save()

        record_audit(
            action=AuditAction.UPDATE,
            entity=tenant,
            actor=request.user,
            request=request,
            before=before,
            after={
                "name": tenant.name,
                "vat_mode": tenant.vat_mode,
                "kra_pin": tenant.kra_pin,
                "receipt_header": tenant.receipt_header,
                "receipt_footer": tenant.receipt_footer,
            },
        )
        return Response(TenantSerializer(tenant).data)


@extend_schema(tags=["tenant"])
class TenantModuleListView(APIView):
    """Which optional capabilities this business has switched on."""

    permission_classes = [IsTenantUser]
    serializer_class = TenantModuleSerializer

    @extend_schema(
        summary="Enabled modules",
        description=(
            "Lists every module and whether this business has it on. The till "
            "uses this to decide whether to show table management, appointment "
            "booking, batch entry and so on."
        ),
        responses={200: TenantModuleSerializer(many=True)},
    )
    def get(self, request):
        modules = TenantModule.objects.filter(tenant=request.user.tenant).order_by("module_key")
        return Response(TenantModuleSerializer(modules, many=True).data)


@extend_schema(tags=["tenant"])
class BusinessTemplateListView(APIView):
    """The business-type templates offered by the setup wizard."""

    permission_classes = [IsAuthenticated]
    serializer_class = BusinessTemplateSerializer

    @extend_schema(
        summary="Business type templates",
        description=(
            "The choices presented on the first screen of setup, with what "
            "each one switches on. Templates only supply defaults; everything "
            "they set stays editable afterwards."
        ),
        responses={200: BusinessTemplateSerializer(many=True)},
    )
    def get(self, request):
        return Response(BusinessTemplateSerializer.all_templates())


@extend_schema(tags=["tenant"])
class TenantSetupView(APIView):
    """First-time setup, run once by the business owner."""

    permission_classes = [IsOwner]
    serializer_class = TenantSetupSerializer

    @extend_schema(
        summary="Complete first-time setup",
        description=(
            "Records how the business prices, creates its first branch and tax "
            "rate, and adds staff accounts with optional till PINs.\n\n"
            "Runs once. A second attempt returns 409, because re-running it "
            "would create a duplicate branch and reset a tax rate that "
            "existing sales already reference."
        ),
        request=TenantSetupSerializer,
        responses={
            200: TenantSerializer,
            409: OpenApiResponse(description="Setup has already been completed"),
        },
    )
    def post(self, request):
        serializer = TenantSetupSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            tenant = complete_setup(
                tenant=request.user.tenant,
                business_type=data["business_type"],
                vat_mode=data["vat_mode"],
                store_name=data["store_name"],
                store_code=data["store_code"],
                tax_rate_name=data["tax_rate_name"],
                tax_rate_bps=data["tax_rate_bps"],
                tax_is_inclusive=data["tax_is_inclusive"],
                staff=data.get("staff", []),
                actor=request.user,
            )
        except SetupAlreadyCompleted as exc:
            return Response(
                {"detail": str(exc), "code": "setup_already_completed"},
                status=status.HTTP_409_CONFLICT,
            )

        tenant.refresh_from_db()
        return Response(TenantSerializer(tenant).data, status=status.HTTP_200_OK)
