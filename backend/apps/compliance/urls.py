"""Routes under /api/v1/ for tax settings and exports."""

from django.urls import path

from apps.compliance.views import (
    ComplianceDocumentListView,
    ComplianceExportView,
    ComplianceSettingsView,
)

urlpatterns = [
    path(
        "compliance/settings/",
        ComplianceSettingsView.as_view(),
        name="compliance-settings",
    ),
    path("compliance/export/", ComplianceExportView.as_view(), name="compliance-export"),
    # A separate path rather than ?format=pdf: DRF reserves ``format`` for
    # content negotiation, so an endpoint taking one answers 404. The receipt
    # endpoints are split the same way, for the same reason.
    path(
        "compliance/export/pdf/",
        ComplianceExportView.as_view(as_pdf=True),
        name="compliance-export-pdf",
    ),
    path(
        "compliance/documents/",
        ComplianceDocumentListView.as_view(),
        name="compliance-documents",
    ),
]
