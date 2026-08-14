"""
Turning a sale into something a customer can hold.

Two renderings from one source of truth, so they cannot disagree about what was
sold:

* **Text**, laid out for a 58mm thermal printer - the width every till printer
  in a Kenyan shop actually is. The Flutter app sends this over Bluetooth as
  ESC/POS.
* **PDF**, for emailing, filing, or printing on ordinary paper when the thermal
  printer has run out.

Both read the sale's own snapshotted lines rather than looking anything up
through the item, so a receipt reprinted next year shows the price that was
charged rather than today's.

Branding comes from the fields captured during setup: name, logo, header line,
footer line and tax PIN. Nothing new to configure.
"""

from __future__ import annotations

import io
from decimal import Decimal

from apps.core.money import from_cents

#: Characters across a 58mm thermal printer at the usual font. Everything in the
#: text receipt is laid out to this width.
THERMAL_WIDTH = 32


def _money(cents: int, currency: str = "") -> str:
    prefix = f"{currency} " if currency else ""
    return f"{prefix}{from_cents(cents):,.2f}"


def _quantity(value: Decimal) -> str:
    """Trim trailing zeros, so 1.000 reads as 1 and 2.500 as 2.5."""
    text = f"{value:f}"
    if "." not in text:
        return text
    trimmed = text.rstrip("0").rstrip(".")
    return trimmed or "0"


def _line(left: str, right: str, width: int = THERMAL_WIDTH) -> str:
    """One row with the label left and the figure right.

    Amounts are right-aligned because that is how a person adds up a column,
    and a receipt whose figures do not line up is one a customer cannot check.
    """
    space = width - len(right)
    if len(left) >= space:
        left = left[: max(0, space - 1)]
    return f"{left.ljust(width - len(right))}{right}"


def _centred(text: str, width: int = THERMAL_WIDTH) -> str:
    return text.center(width)[:width]


def _rule(character: str = "-", width: int = THERMAL_WIDTH) -> str:
    return character * width


def render_text(sale, *, width: int = THERMAL_WIDTH) -> str:
    """A receipt as plain text, laid out for a thermal printer."""
    tenant = sale.tenant
    currency = tenant.currency or "KES"
    rows: list[str] = []

    rows.append(_centred(tenant.name.upper(), width))
    if tenant.receipt_header:
        rows.append(_centred(tenant.receipt_header, width))
    if tenant.phone:
        rows.append(_centred(tenant.phone, width))
    if tenant.kra_pin:
        rows.append(_centred(f"PIN: {tenant.kra_pin}", width))
    rows.append(_rule("=", width))

    # The fiscal number when there is one, and the till's own reference when the
    # sale was rung up with no connection. Both are printed after a sync so the
    # paper a customer is holding can be matched to the number tax reporting
    # reads.
    if sale.receipt_code:
        rows.append(_line("Receipt", sale.receipt_code, width))
    if sale.provisional_reference:
        rows.append(_line("Till ref", sale.provisional_reference, width))

    stamp = sale.device_created_at or sale.created_at
    rows.append(_line("Date", stamp.strftime("%d/%m/%Y %H:%M"), width))
    rows.append(_line("Served by", sale.cashier.get_short_name(), width))
    rows.append(_line("Branch", sale.store.code, width))
    rows.append(_rule("-", width))

    for line in sale.lines.all():
        rows.append(line.name[:width])
        quantity = _quantity(line.quantity)
        detail = f"  {quantity} x {_money(line.unit_price_cents)}"
        rows.append(_line(detail, _money(line.gross_cents), width))

        if line.total_discount_cents:
            rows.append(
                _line("  Discount", f"-{_money(line.total_discount_cents)}", width)
            )

    rows.append(_rule("-", width))

    if sale.discount_cents:
        gross_before_discount = (
            sale.subtotal_cents + sale.tax_cents + sale.discount_cents
        )
        rows.append(_line("Subtotal", _money(gross_before_discount), width))
        rows.append(_line("Discount", f"-{_money(sale.discount_cents)}", width))

    rows.append(_line("Net", _money(sale.subtotal_cents), width))

    # Tax is broken out per rate, because a customer with a mixed basket needs
    # to see which part carried VAT and a business needs it for filing.
    for rate_bps, tax_cents in _tax_by_rate(sale).items():
        label = "VAT" if rate_bps else "Zero rated"
        if rate_bps:
            label = f"VAT {rate_bps / 100:g}%"
        rows.append(_line(label, _money(tax_cents), width))

    rows.append(_rule("=", width))
    rows.append(_line("TOTAL", _money(sale.total_cents, currency), width))

    if sale.rounding_adjustment_cents:
        # Shown rather than hidden: a customer handed a figure different from
        # the total is owed an explanation of the difference.
        rows.append(
            _line("Rounding", _money(sale.rounding_adjustment_cents), width)
        )
        rows.append(_line("TO PAY", _money(sale.amount_due_cents, currency), width))

    rows.append("")

    for payment in sale.payments.all():
        label = "Cash" if payment.method == "CASH" else "M-Pesa"
        rows.append(_line(label, _money(payment.amount_cents), width))
        if payment.method == "CASH" and payment.tendered_cents:
            rows.append(_line("  Tendered", _money(payment.tendered_cents), width))
            rows.append(_line("  Change", _money(payment.change_cents), width))
        if payment.mpesa_receipt_number:
            rows.append(f"  Ref {payment.mpesa_receipt_number}"[:width])

    refunds = list(sale.refunds.all())
    if refunds:
        rows.append(_rule("-", width))
        rows.append("REFUNDS")
        for refund in refunds:
            rows.append(
                _line(
                    refund.created_at.strftime("%d/%m/%Y"),
                    f"-{_money(refund.amount_cents)}",
                    width,
                )
            )

    rows.append(_rule("=", width))
    if tenant.receipt_footer:
        rows.append(_centred(tenant.receipt_footer, width))
    rows.append("")

    return "\n".join(rows)


def _tax_by_rate(sale) -> dict[int, int]:
    """Tax grouped by the rate that produced it.

    Grouped rather than totalled because a basket can mix rates - and it does,
    constantly, since unprocessed foods are zero-rated and much else is not.
    """
    grouped: dict[int, int] = {}
    for line in sale.lines.all():
        if line.tax_cents or line.tax_rate_bps:
            grouped[line.tax_rate_bps] = grouped.get(line.tax_rate_bps, 0) + line.tax_cents
    return dict(sorted(grouped.items()))


def render_pdf(sale) -> bytes:
    """The same receipt as a PDF, on a narrow page.

    Sized like a till roll rather than A4, so a shop can print it on either
    without the text stranded in the top-left corner of a mostly empty sheet.
    """
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    text = render_text(sale)
    lines = text.split("\n")

    page_width = 80 * mm
    line_height = 4.2 * mm
    margin = 5 * mm
    page_height = max(120 * mm, (len(lines) + 6) * line_height)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    pdf.setTitle(f"Receipt {sale.receipt_code or sale.provisional_reference or sale.pk}")

    # Monospaced, because the text layout aligns figures by padding with spaces
    # and a proportional font would throw every column out.
    pdf.setFont("Courier", 8.5)

    y = page_height - margin
    for line in lines:
        pdf.drawString(margin, y, line)
        y -= line_height

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def receipt_filename(sale) -> str:
    reference = sale.receipt_code or sale.provisional_reference or str(sale.pk)[:8]
    return f"receipt-{reference}.pdf"
