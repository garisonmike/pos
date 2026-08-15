"""
Routes under /api/v1/ for tax settings and exports.

**One route per content type, never a ``?format=`` parameter.** DRF reserves
``format`` for its own content negotiation and answers 404 on a value it does
not recognise, rather than ignoring it - so such an endpoint looks like a
missing URL instead of a bad request. That has now cost a 404 twice here: on
the sale receipt, and again on this export. The same note is on the receipt
actions in apps/sales/views.py.
"""

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
    # A separate path rather than ?format=pdf - see the note at the top.
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
