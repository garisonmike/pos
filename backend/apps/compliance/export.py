"""
What a business types into eTIMS Lite.

The manual adapter's whole output. Somebody sits down with this and enters it,
so the columns are the ones that form asks for and nothing else - an export
carrying fields nobody needs is an export somebody has to read past.

Both renderings come from one function that builds the rows, so the CSV and the
PDF cannot disagree about what was declared.
"""

from __future__ import annotations

import csv
import io

from apps.compliance.models import ComplianceDocument, DocumentKind
from apps.core.money import from_cents

COLUMNS = [
    "Document",
    "Type",
    "Date",
    "Seller PIN",
    "Buyer PIN",
    "Rate",
    "Net",
    "Tax",
    "Gross",
]


def _rate_label(rate_bps: int) -> str:
    """A rate as a filer would write it."""
    if rate_bps == 0:
        return "Zero-rated"
    return f"{rate_bps / 100:g}%"


def rows_for(documents) -> list[list[str]]:
    """One row per rate per document.

    Split by rate rather than one row per document, because a return is filled
    in per rate - and a sale mixing 16% sugar with zero-rated bread, which a
    duka does constantly, would otherwise need the filer to do the splitting by
    hand from a single total.

    A credit note's figures are written **negative** here, even though they are
    stored positive. On the form they subtract; in the database a negative
    number on a document already called a credit note invites somebody to
    subtract it twice.
    """
    rows = []
    for document in documents:
        sign = -1 if document.kind == DocumentKind.CREDIT_NOTE else 1
        breakdown = document.tax_breakdown or [
            {
                "rate_bps": 0,
                "net_cents": document.net_cents,
                "tax_cents": document.tax_cents,
                "gross_cents": document.gross_cents,
            }
        ]
        for bucket in breakdown:
            rows.append(
                [
                    document.invoice_code,
                    "Credit note" if sign < 0 else "Tax invoice",
                    document.issued_at.strftime("%Y-%m-%d"),
                    document.seller_pin,
                    document.buyer_pin,
                    _rate_label(bucket.get("rate_bps", 0)),
                    f"{from_cents(sign * bucket.get('net_cents', 0)):.2f}",
                    f"{from_cents(sign * bucket.get('tax_cents', 0)):.2f}",
                    f"{from_cents(sign * bucket.get('gross_cents', 0)):.2f}",
                ]
            )
    return rows


def render_csv(documents) -> str:
    """The export as a spreadsheet.

    ``\\r\\n`` line endings, because this is opened in Excel on Windows more
    often than anywhere else and bare newlines put the whole file on one row.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows_for(documents))
    return buffer.getvalue()


def render_pdf(documents, *, business_name: str = "") -> bytes:
    """The same rows, for a filer who would rather work from paper."""
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
        title="Tax documents",
    )
    styles = getSampleStyleSheet()

    story = [
        Paragraph(business_name or "Tax documents", styles["Title"]),
        Paragraph(
            "Every figure as recorded at the moment of issue. A credit note is "
            "shown negative, as it is entered.",
            styles["Normal"],
        ),
        Spacer(1, 6 * mm),
    ]

    data = [COLUMNS] + rows_for(documents)
    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#00695C")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D3D8DC")),
                # Figures right-aligned, because that is how a person adds up a
                # column and this exists to be added up.
                ("ALIGN", (6, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(table)

    document.build(story)
    return buffer.getvalue()


def documents_for_export(*, tenant, since=None, until=None):
    """The documents a filing period covers.

    Ordered by invoice number rather than by date, because the number is the
    gapless series and reading it in order is how a filer sees that nothing is
    missing.
    """
    queryset = ComplianceDocument.objects.filter(tenant=tenant)
    if since:
        queryset = queryset.filter(issued_at__gte=since)
    if until:
        queryset = queryset.filter(issued_at__lt=until)
    return queryset.order_by("invoice_number")
