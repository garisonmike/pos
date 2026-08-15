"""
Routes under /api/v1/ for reporting.

**One route per content type, never a ``?format=`` parameter.** DRF reserves
``format`` for content negotiation and answers 404 on a value it does not
recognise, so such a route reads as a missing URL rather than a bad request.
That has cost a 404 twice already here - the sale receipt and the compliance
export - and the note lives beside those routes too.
"""

from django.urls import path

from apps.reports.views import (
    BestSellersReportView,
    CashierReportView,
    DrawerReportView,
    RefundReportView,
    SalesReportView,
)

urlpatterns = [
    path("reports/sales/", SalesReportView.as_view(), name="report-sales"),
    path(
        "reports/sales/csv/",
        SalesReportView.as_view(as_csv=True),
        name="report-sales-csv",
    ),
    path(
        "reports/sales/pdf/",
        SalesReportView.as_view(as_pdf=True),
        name="report-sales-pdf",
    ),
    path("reports/best-sellers/", BestSellersReportView.as_view(), name="report-best"),
    path(
        "reports/best-sellers/csv/",
        BestSellersReportView.as_view(as_csv=True),
        name="report-best-csv",
    ),
    path("reports/cashiers/", CashierReportView.as_view(), name="report-cashiers"),
    path(
        "reports/cashiers/csv/",
        CashierReportView.as_view(as_csv=True),
        name="report-cashiers-csv",
    ),
    path("reports/refunds/", RefundReportView.as_view(), name="report-refunds"),
    path("reports/drawers/", DrawerReportView.as_view(), name="report-drawers"),
    path(
        "reports/drawers/csv/",
        DrawerReportView.as_view(as_csv=True),
        name="report-drawers-csv",
    ),
]
