"""
Reports as a file.

Every row comes from the same query layer the API reads, so a CSV and the screen
it was downloaded from cannot disagree - the arrangement the receipt renderings
and the compliance export both use.

Figures are written as money rather than cents, because nobody reads a report in
cents, and rates are rendered from basis points here at the last moment rather
than stored as floats anywhere upstream.
"""

from __future__ import annotations

import csv
import io

from apps.core.money import from_cents


def _money(cents: int) -> str:
    return f"{from_cents(cents):.2f}"


def _rate(bps: int) -> str:
    return f"{bps / 100:.2f}%"


SALES_COLUMNS = [
    "Period",
    "Sales",
    "Gross",
    "Net",
    "VAT",
    "Discounts",
    "Cash in",
    "M-Pesa in",
    "Refunded",
    "Net taken",
    "Average basket",
    "Refund rate",
    "Voids",
]


def sales_rows(summaries) -> list[list[str]]:
    return [
        [
            summary.period.label,
            str(summary.sale_count),
            _money(summary.gross_cents),
            _money(summary.net_cents),
            _money(summary.tax_cents),
            _money(summary.discount_cents),
            _money(summary.taken.cash_cents),
            _money(summary.taken.mpesa_cents),
            _money(summary.refunded.total_cents),
            _money(summary.net_taken_cents),
            _money(summary.average_basket_cents),
            _rate(summary.refund_rate_bps),
            str(summary.void_count),
        ]
        for summary in summaries
    ]


BEST_SELLER_COLUMNS = ["Item", "SKU", "Quantity", "Revenue", "Lines"]


def best_seller_rows(sellers) -> list[list[str]]:
    return [
        [
            seller.name,
            seller.sku,
            str(seller.quantity),
            _money(seller.revenue_cents),
            str(seller.line_count),
        ]
        for seller in sellers
    ]


CASHIER_COLUMNS = [
    "Cashier",
    "Sales",
    "Gross",
    "Average basket",
    "Discounted sales",
    "Discount rate",
    "Voids",
    "Refunds",
]


def cashier_rows(figures) -> list[list[str]]:
    """Denominators alongside every rate.

    A discount rate on its own invites a conclusion the figure does not
    support. Printing the sale count and the number of discounted sales beside
    it means whoever reads this can see how thin the evidence is.
    """
    return [
        [
            figure.full_name or figure.username,
            str(figure.sale_count),
            _money(figure.gross_cents),
            _money(figure.average_basket_cents),
            str(figure.discounted_sale_count),
            _rate(figure.discount_rate_bps),
            str(figure.void_count),
            str(figure.refund_count),
        ]
        for figure in figures
    ]


DRAWER_COLUMNS = [
    "Shift",
    "Cashier",
    "Opened",
    "Float",
    "Counted",
    "Expected",
    "Variance",
    "Arrived after close",
    "Variance if those had landed in time",
]


def drawer_rows(reconciliations) -> list[list[str]]:
    """The counted figures and the late arrivals, side by side.

    Never merged. The last column is an explanation of the gap, not a
    correction to it - the variance column stays exactly as it was signed for.
    """
    rows = []
    for row in reconciliations:
        rows.append(
            [
                row.shift_id[:8],
                row.cashier,
                row.opened_at.strftime("%Y-%m-%d %H:%M") if row.opened_at else "",
                _money(row.opening_float_cents),
                _money(row.declared_closing_cents)
                if row.declared_closing_cents is not None
                else "(open)",
                _money(row.expected_closing_cents)
                if row.expected_closing_cents is not None
                else "",
                _money(row.variance_cents) if row.variance_cents is not None else "",
                _money(row.late_cash_cents) if row.late_count else "",
                _money(row.explained_variance_cents)
                if row.explained_variance_cents is not None and row.late_count
                else "",
            ]
        )
    return rows


def render_csv(columns: list[str], rows: list[list[str]]) -> str:
    """Windows line endings, because this is opened in Excel more than anywhere."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def render_pdf(
    *, title: str, subtitle: str, columns: list[str], rows: list[list[str]]
) -> bytes:
    """The same rows on paper."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()

    story = [Paragraph(title, styles["Title"])]
    if subtitle:
        story.append(Paragraph(subtitle, styles["Normal"]))
    story.append(Spacer(1, 6 * mm))

    table = Table([columns] + rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#00695C")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D3D8DC")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(table)
    document.build(story)
    return buffer.getvalue()
