"""
Reporting endpoints.

Manager and above throughout. A cashier's job is the counter; what the business
took last month is not theirs to read, and cashier performance least of all.

**One route per content type, never a ``?format=`` parameter.** DRF reserves
``format`` for its own content negotiation and answers 404 on a value it does
not recognise, rather than ignoring it - so such a route looks like a missing
URL instead of a bad request. That has already cost a 404 twice in this
codebase; see the note on the receipt actions in apps/sales/views.py.
"""

from __future__ import annotations

from datetime import date

from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManagerOrAbove, IsPlatformAdmin
from apps.reports import export
from apps.reports.drawer import (
    cash_taken_in,
    drawer_totals,
    shift_summary,
    unreconciled_shift_count,
)
from apps.reports.periods import (
    DAY,
    GRANULARITIES,
    PeriodError,
    period_for,
    periods_between,
)
from apps.reports.platform import platform_totals, usage_summary
from apps.reports.queries import (
    BY_QUANTITY,
    BY_REVENUE,
    best_sellers,
    cashier_figures,
    refund_reasons,
    sales_series,
    sales_summary,
)


def _read_period(request, tenant):
    """The window a request is asking about, or an error explaining why not.

    Refused rather than defaulted when unreadable: silently falling back to
    today would answer a different question than the one asked, and look like
    it worked.
    """
    params = request.query_params
    granularity = (params.get("granularity") or DAY).lower()
    if granularity not in GRANULARITIES:
        return None, None, Response(
            {
                "detail": f"Granularity must be one of {', '.join(GRANULARITIES)}.",
                "code": "bad_granularity",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    on, error = _date(params.get("on"), "on")
    if error is not None:
        return None, None, error
    since, error = _date(params.get("since"), "since")
    if error is not None:
        return None, None, error
    until, error = _date(params.get("until"), "until")
    if error is not None:
        return None, None, error

    try:
        if since or until:
            periods = periods_between(
                tenant,
                granularity=granularity,
                since=since or until,
                until=until or since,
            )
        else:
            periods = [period_for(tenant, granularity=granularity, on=on)]
    except PeriodError as exc:
        return None, None, Response(
            {"detail": str(exc), "code": "bad_period"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return periods, granularity, None


def _date(raw, name):
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, Response(
            {"detail": f"{name} must be a date like 2026-08-15.", "code": f"bad_{name}"},
            status=status.HTTP_400_BAD_REQUEST,
        )


def _download(*, as_pdf, title, subtitle, columns, rows, filename):
    if as_pdf:
        body = export.render_pdf(
            title=title, subtitle=subtitle, columns=columns, rows=rows
        )
        response = HttpResponse(body, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
        return response

    response = HttpResponse(export.render_csv(columns, rows), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    return response


class _ReportView(APIView):
    permission_classes = [IsManagerOrAbove]

    #: Set on the download routes. A separate path, not a query parameter.
    as_csv = False
    as_pdf = False

    @property
    def is_download(self) -> bool:
        return self.as_csv or self.as_pdf


@extend_schema(tags=["reports"])
class SalesReportView(_ReportView):
    """What the business took."""

    @extend_schema(
        summary="Sales summary",
        parameters=[
            OpenApiParameter(name="granularity", description="day, week or month."),
            OpenApiParameter(name="on", description="A date inside the period."),
            OpenApiParameter(name="since", description="Start of a range, inclusive."),
            OpenApiParameter(name="until", description="End of a range, inclusive."),
        ],
        responses={200: None},
    )
    def get(self, request):
        tenant = request.user.tenant
        periods, granularity, error = _read_period(request, tenant)
        if error is not None:
            return error

        summaries = sales_series(tenant, periods)

        if self.is_download:
            return _download(
                as_pdf=self.as_pdf,
                title=f"{tenant.name} - sales",
                subtitle=f"By {granularity}. Figures as recorded by the server.",
                columns=export.SALES_COLUMNS,
                rows=export.sales_rows(summaries),
                filename=f"sales-{tenant.slug}",
            )

        return Response(
            {
                "granularity": granularity,
                "periods": [summary.as_dict() for summary in summaries],
            }
        )


@extend_schema(tags=["reports"])
class BestSellersReportView(_ReportView):
    """What actually moved."""

    @extend_schema(
        summary="Best sellers",
        parameters=[
            OpenApiParameter(name="granularity", description="day, week or month."),
            OpenApiParameter(name="on", description="A date inside the period."),
            OpenApiParameter(
                name="order",
                description=(
                    "revenue (default) or quantity. They rank differently and a "
                    "shop needs both readings."
                ),
            ),
        ],
        responses={200: None},
    )
    def get(self, request):
        tenant = request.user.tenant
        periods, granularity, error = _read_period(request, tenant)
        if error is not None:
            return error

        order = (request.query_params.get("order") or BY_REVENUE).lower()
        if order not in (BY_QUANTITY, BY_REVENUE):
            return Response(
                {"detail": "Order by revenue or quantity.", "code": "bad_order"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        period = periods[0] if len(periods) == 1 else _span(periods)
        sellers = best_sellers(tenant, period, order=order)

        if self.is_download:
            return _download(
                as_pdf=self.as_pdf,
                title=f"{tenant.name} - best sellers",
                subtitle=f"{period.label}, ranked by {order}.",
                columns=export.BEST_SELLER_COLUMNS,
                rows=export.best_seller_rows(sellers),
                filename=f"best-sellers-{tenant.slug}",
            )

        return Response(
            {
                **period.as_dict(),
                "order": order,
                "items": [seller.as_dict() for seller in sellers],
            }
        )


@extend_schema(tags=["reports"])
class CashierReportView(_ReportView):
    """Per-person figures, with their denominators.

    Read the denominators, not the headline. See ARCHITECTURE.md: this exists to
    find a pattern worth asking about, not to rank people.
    """

    @extend_schema(summary="Cashier figures", responses={200: None})
    def get(self, request):
        tenant = request.user.tenant
        periods, _granularity, error = _read_period(request, tenant)
        if error is not None:
            return error

        period = periods[0] if len(periods) == 1 else _span(periods)
        figures = cashier_figures(tenant, period)

        if self.is_download:
            return _download(
                as_pdf=self.as_pdf,
                title=f"{tenant.name} - cashiers",
                subtitle=(
                    f"{period.label}. Rates are shown beside the counts they "
                    "come from; a rate on its own supports no conclusion."
                ),
                columns=export.CASHIER_COLUMNS,
                rows=export.cashier_rows(figures),
                filename=f"cashiers-{tenant.slug}",
            )

        return Response(
            {
                **period.as_dict(),
                "cashiers": [figure.as_dict() for figure in figures],
                "note": (
                    "Rates are shown beside the counts they come from. A cashier "
                    "on a quiet shift will always look different, and a discount "
                    "rate says nothing without knowing who authorised each one."
                ),
            }
        )


@extend_schema(tags=["reports"])
class RefundReportView(_ReportView):
    """What went back, and why."""

    @extend_schema(summary="Refunds", responses={200: None})
    def get(self, request):
        tenant = request.user.tenant
        periods, _granularity, error = _read_period(request, tenant)
        if error is not None:
            return error

        period = periods[0] if len(periods) == 1 else _span(periods)
        summary = sales_summary(tenant, period)

        return Response(
            {
                **period.as_dict(),
                "refunded": summary.refunded.as_dict(),
                "refund_count": summary.refund_count,
                "refund_rate_bps": summary.refund_rate_bps,
                "gross_cents": summary.gross_cents,
                "reasons": [reason.as_dict() for reason in refund_reasons(tenant, period)],
            }
        )


@extend_schema(tags=["reports"])
class DrawerReportView(_ReportView):
    """Shifts as counted, beside what arrived after they closed.

    The report a manager reads when today's sales figure disagrees with the
    drawer. Both numbers are right; this shows why they differ without merging
    them into a third number that is neither.
    """

    @extend_schema(summary="Shift and drawer summary", responses={200: None})
    def get(self, request):
        tenant = request.user.tenant
        periods, _granularity, error = _read_period(request, tenant)
        if error is not None:
            return error

        period = periods[0] if len(periods) == 1 else _span(periods)
        reconciliations = shift_summary(tenant, period)
        totals = drawer_totals(reconciliations)

        if self.is_download:
            return _download(
                as_pdf=self.as_pdf,
                title=f"{tenant.name} - drawers",
                subtitle=(
                    f"{period.label}. Counted figures are as signed for and are "
                    "never recomputed; late arrivals are shown separately."
                ),
                columns=export.DRAWER_COLUMNS,
                rows=export.drawer_rows(reconciliations),
                filename=f"drawers-{tenant.slug}",
            )

        return Response(
            {
                **period.as_dict(),
                "shifts": [row.as_dict() for row in reconciliations],
                "totals": totals.as_dict(),
                # The figure the drawers are compared against. It can exceed
                # what they were counted at, and the late arrivals above are
                # why.
                "cash_taken_in_period_cents": cash_taken_in(tenant, period),
                "unreconciled_shift_count": unreconciled_shift_count(tenant),
                "note": (
                    "Counted figures are frozen at close and are never "
                    "recomputed. Anything that arrived afterwards is listed "
                    "beside them, not added into them."
                ),
            }
        )


@extend_schema(tags=["platform"])
class PlatformTradingView(APIView):
    """What every business actually sold in a period, for invoicing.

    Distinct from ``PlatformUsageView`` in apps.platform_admin, which counts
    *structure* - staff, branches, tills, catalogue size - and deliberately
    carries no money. That view predates there being any sales to count. This
    one is the trading side: sale counts and revenue for a period, which is
    what a usage-based invoice keys on.

    Platform administrators only. Reads across tenants through the usual
    bypass, kept to the queries themselves.
    """

    permission_classes = [IsPlatformAdmin]

    @extend_schema(
        summary="Per-tenant usage",
        parameters=[
            OpenApiParameter(name="granularity", description="day, week or month."),
            OpenApiParameter(name="on", description="A date inside the period."),
        ],
        responses={200: None},
    )
    def get(self, request):
        # A platform administrator belongs to no business, so the period is
        # built against no tenant and falls back to the platform's own zone.
        periods, granularity, error = _read_period(request, None)
        if error is not None:
            return error

        period = periods[0]
        rows = usage_summary(period)

        return Response(
            {
                **period.as_dict(),
                "granularity": granularity,
                "tenants": [row.as_dict() for row in rows],
                "totals": platform_totals(rows).as_dict(),
            }
        )


def _span(periods):
    """One window covering several periods.

    Used where a report is a ranking rather than a series - a best-seller list
    across three months is one list, not three.
    """
    from apps.reports.periods import Period

    return Period(
        start=periods[0].start,
        end=periods[-1].end,
        label=f"{periods[0].label} to {periods[-1].label}",
        granularity=periods[0].granularity,
    )
