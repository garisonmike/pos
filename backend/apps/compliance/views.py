"""
The back-office surface for tax settings and exports.

Two endpoints, with deliberately different gates.

**Settings are Owner-only.** Changing the compliance mode is not a preference;
it decides whether a business declares tax at all. Getting it wrong in one
direction means declaring tax that is not owed, and in the other means failing
to declare tax that is - both of which land on the owner, not on whoever
happened to be managing that afternoon. It sits *above* Manager for the same
reason a refund does not.

**The export is Manager-and-above.** It is read-only, it is the back office's
own filing work, and a manager doing the monthly return should not have to
borrow the owner's account to download it.
"""

from __future__ import annotations

from django.db import transaction
from django.http import HttpResponse
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.compliance.export import documents_for_export, render_csv, render_pdf
from apps.compliance.models import (
    ComplianceDocument,
    ComplianceMode,
    InvoiceCounter,
    SubmissionState,
)
from apps.compliance.numbering import peek_next_invoice_number
from apps.compliance.serializers import (
    ComplianceDocumentSerializer,
    ComplianceSettingsSerializer,
)
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.permissions import IsManagerOrAbove, IsOwner, IsTenantUser


def _counter_for(tenant) -> InvoiceCounter:
    counter, _created = InvoiceCounter.objects.get_or_create(
        tenant=tenant, defaults={"last_number": 0}
    )
    return counter


@extend_schema(tags=["compliance"])
class ComplianceSettingsView(APIView):
    """Read and change a business's tax setup."""

    serializer_class = ComplianceSettingsSerializer

    def get_permissions(self):
        """Anyone in the business may look; only the owner may change it.

        Reading matters because a till needs to know whether to ask for a
        buyer PIN at all. Changing it is a legal decision about what the
        business declares.
        """
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsTenantUser()]
        return [IsOwner()]

    def _payload(self, tenant) -> dict:
        counter = _counter_for(tenant)
        mode = tenant.compliance_mode
        return {
            "compliance_mode": mode,
            "mode_label": dict(ComplianceMode.choices).get(mode, "Not recognised"),
            "kra_pin": tenant.kra_pin,
            "invoice_prefix": counter.prefix,
            "next_invoice_number": peek_next_invoice_number(tenant),
        }

    @extend_schema(
        summary="Own tax setup",
        responses={200: ComplianceSettingsSerializer},
    )
    def get(self, request):
        return Response(self._payload(request.user.tenant))

    @extend_schema(
        summary="Change the tax setup",
        description=(
            "Owner only. Changing the compliance mode decides whether this "
            "business declares tax at all, so the change is audited with the "
            "old and new value and the person who made it."
        ),
        request=ComplianceSettingsSerializer,
        responses={200: ComplianceSettingsSerializer},
    )
    def patch(self, request):
        serializer = ComplianceSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant = request.user.tenant
        counter = _counter_for(tenant)

        before = {
            "compliance_mode": tenant.compliance_mode,
            "kra_pin": tenant.kra_pin,
            "invoice_prefix": counter.prefix,
        }

        if "invoice_prefix" in data and counter.last_number > 0:
            # Refused rather than applied. Changing the prefix mid-series would
            # produce two spellings of one gapless sequence, and a filer
            # reading it would not be able to tell whether something was
            # missing between them.
            if data["invoice_prefix"] != counter.prefix:
                return Response(
                    {
                        "detail": (
                            "This business has already issued invoices under "
                            f"'{counter.prefix}'. Changing the prefix now would "
                            "split one series into two spellings."
                        ),
                        "code": "series_already_started",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            if "compliance_mode" in data:
                tenant.compliance_mode = data["compliance_mode"]
            if "kra_pin" in data:
                tenant.kra_pin = (data["kra_pin"] or "").strip().upper()
            tenant.save(update_fields=["compliance_mode", "kra_pin", "updated_at"])

            if "invoice_prefix" in data:
                counter.prefix = data["invoice_prefix"]
                counter.save(update_fields=["prefix", "updated_at"])

            after = {
                "compliance_mode": tenant.compliance_mode,
                "kra_pin": tenant.kra_pin,
                "invoice_prefix": counter.prefix,
            }

            # A distinct action for the mode, so a change of tax regime is
            # findable without reading every settings edit the business ever
            # made. The generic UPDATE covers the rest.
            action = (
                AuditAction.COMPLIANCE_MODE_CHANGED
                if before["compliance_mode"] != after["compliance_mode"]
                else AuditAction.UPDATE
            )
            record_audit(
                action=action,
                entity_type="tenants.Tenant",
                entity_id=str(tenant.id),
                tenant_id=tenant.id,
                actor=request.user,
                request=request,
                before=before,
                after=after,
            )

        return Response(self._payload(tenant))


@extend_schema(tags=["compliance"])
class ComplianceExportView(APIView):
    """Download the tax documents for a period.

    Manager and above: this is the back office's own filing work, and a manager
    doing the monthly return should not need the owner's account for it.
    """

    permission_classes = [IsManagerOrAbove]

    #: Set on the PDF route. Not a query parameter, because DRF reserves
    #: ``format`` for its own content negotiation and an endpoint that takes
    #: one answers 404 - which this file learned the same way the receipt
    #: endpoint did, by shipping it.
    as_pdf = False

    @extend_schema(
        summary="Download tax documents",
        parameters=[
            OpenApiParameter(
                name="since",
                description="ISO timestamp. Inclusive.",
                required=False,
                type=str,
            ),
            OpenApiParameter(
                name="until",
                description="ISO timestamp. Exclusive, so months do not overlap.",
                required=False,
                type=str,
            ),
        ],
        responses={200: None},
    )
    def get(self, request):
        tenant = request.user.tenant

        since, error = _timestamp(request, "since")
        if error is not None:
            return error
        until, error = _timestamp(request, "until")
        if error is not None:
            return error

        documents = documents_for_export(tenant=tenant, since=since, until=until)

        if self.as_pdf:
            body = render_pdf(documents, business_name=tenant.name)
            response = HttpResponse(body, content_type="application/pdf")
            response["Content-Disposition"] = (
                f'attachment; filename="tax-documents-{tenant.slug}.pdf"'
            )
            return response

        response = HttpResponse(render_csv(documents), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="tax-documents-{tenant.slug}.csv"'
        )
        return response


@extend_schema(tags=["compliance"])
class ComplianceDocumentListView(APIView):
    """The documents themselves, for a back-office screen.

    Read-only everywhere. A document is immutable once issued, and an endpoint
    that appeared to edit one would be a lie about what the system does.
    """

    permission_classes = [IsManagerOrAbove]
    serializer_class = ComplianceDocumentSerializer

    @extend_schema(
        summary="List tax documents",
        parameters=[
            OpenApiParameter(
                name="state",
                description="Filter by submission state.",
                required=False,
                type=str,
            ),
        ],
        responses={200: ComplianceDocumentSerializer(many=True)},
    )
    def get(self, request):
        documents = ComplianceDocument.objects.filter(
            tenant=request.user.tenant
        ).select_related("sale")

        state = request.query_params.get("state")
        if state:
            if state not in SubmissionState.values:
                return Response(
                    {"detail": "No such submission state.", "code": "bad_state"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            documents = documents.filter(submission_state=state)

        return Response(
            ComplianceDocumentSerializer(
                documents.order_by("-issued_at")[:500], many=True
            ).data
        )


def _timestamp(request, name):
    """Read an ISO timestamp from the query string, or explain why not.

    Refused rather than ignored: silently dropping an unreadable ``since``
    would hand somebody a full export where they asked for one month, and it
    would look like it worked.
    """
    raw = request.query_params.get(name)
    if not raw:
        return None, None

    parsed = parse_datetime(raw)
    if parsed is None:
        return None, Response(
            {"detail": f"{name} must be an ISO timestamp.", "code": f"bad_{name}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from django.utils import timezone

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed, None
