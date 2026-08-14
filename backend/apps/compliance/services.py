"""
Turning a settled sale into a tax document.

Called from the checkout view and from sync replay, **inside the transaction
that commits the sale**, so the invoice number rolls back with the sale if
anything fails. There is no deferred path: an online sale is numbered
immediately in its own transaction, and an offline sale is numbered when it
syncs, which is when that sale commits.
"""

from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from apps.compliance.adapters import adapter_for, submit
from apps.compliance.models import (
    ComplianceDocument,
    ComplianceMode,
    DocumentKind,
    SubmissionState,
    looks_like_a_kra_pin,
)
from apps.compliance.numbering import allocate_invoice_number
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.sales.models import Sale, SaleState


class ComplianceError(Exception):
    def __init__(self, detail: str, code: str = "compliance_error"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def tax_breakdown_for(sale: Sale) -> list[dict]:
    """Net and tax per rate, as the document will freeze them.

    Grouped by rate because that is how a return is filled in - a single tax
    total tells a filer nothing when a sale mixes 16% and zero-rated lines,
    which a duka selling both bread and sugar does constantly.
    """
    buckets: dict[int, dict] = defaultdict(
        lambda: {"net_cents": 0, "tax_cents": 0, "gross_cents": 0}
    )
    for line in sale.lines.all():
        bucket = buckets[line.tax_rate_bps]
        bucket["net_cents"] += line.net_cents
        bucket["tax_cents"] += line.tax_cents
        bucket["gross_cents"] += line.gross_cents

    return [
        {"rate_bps": rate, **totals}
        for rate, totals in sorted(buckets.items(), reverse=True)
    ]


def mode_for(tenant) -> str:
    """Which regime a business is under."""
    return getattr(tenant, "compliance_mode", ComplianceMode.NONE)


@transaction.atomic
def issue_invoice(
    *, sale: Sale, buyer_pin: str = "", user=None, request=None
) -> ComplianceDocument:
    """Raise the tax invoice for a settled sale.

    Refuses a sale that is not settled. An invoice for money that has not been
    taken is a declaration of revenue the shop does not have, and a void that
    followed would leave it standing.
    """
    if sale.state not in (SaleState.PAID, SaleState.PARTIALLY_REFUNDED, SaleState.REFUNDED):
        raise ComplianceError(
            "A tax invoice can only be raised for a settled sale.",
            "sale_not_settled",
        )

    existing = ComplianceDocument.objects.filter(
        sale=sale, kind=DocumentKind.INVOICE
    ).first()
    if existing is not None:
        # Not an error. A retried checkout must not raise a second invoice for
        # the same sale, and the caller wants the document either way.
        return existing

    if buyer_pin and not looks_like_a_kra_pin(buyer_pin):
        raise ComplianceError(
            "That does not look like a tax PIN. It should read like P051234567X.",
            "bad_buyer_pin",
        )

    tenant = sale.tenant
    number, code = allocate_invoice_number(tenant)
    breakdown = tax_breakdown_for(sale)
    adapter = adapter_for(mode_for(tenant))

    document = ComplianceDocument.objects.create(
        tenant=tenant,
        sale=sale,
        kind=DocumentKind.INVOICE,
        invoice_number=number,
        invoice_code=code,
        buyer_pin=(buyer_pin or "").strip().upper(),
        seller_pin=tenant.kra_pin or "",
        net_cents=sale.subtotal_cents,
        tax_cents=sale.tax_cents,
        gross_cents=sale.total_cents,
        tax_breakdown=breakdown,
        issued_at=timezone.now(),
        issued_by=user,
        adapter=adapter.name,
        submission_state=(
            SubmissionState.PENDING if adapter.submits else SubmissionState.NOT_REQUIRED
        ),
    )

    # Run it through the adapter now for anything that answers immediately. A
    # gateway that needs the network is queued instead - see submit_pending -
    # because a checkout must never wait on, or fail because of, a government
    # service. The goods have left the shop.
    if not adapter.submits:
        submit(document, adapter)

    record_audit(
        action=AuditAction.CREATE,
        entity=document,
        actor=user,
        request=request,
        tenant_id=tenant.id,
        after={
            "invoice_code": code,
            "gross_cents": sale.total_cents,
            "buyer_pin": bool(buyer_pin),
            "adapter": adapter.name,
        },
    )
    return document


@transaction.atomic
def issue_credit_note(
    *, original: ComplianceDocument, reason: str, user=None, request=None
) -> ComplianceDocument:
    """Correct an invoice by referencing it, never by editing it.

    The figures are those of the original. A partial correction is out of scope
    for this milestone and would want its own line-level shape; raising a full
    credit note and a fresh invoice is the honest way to express one meanwhile,
    and it is what a filer would do on paper.
    """
    if original.is_credit_note:
        raise ComplianceError(
            "A credit note cannot itself be credited.", "already_a_credit_note"
        )
    if not reason.strip():
        raise ComplianceError("A credit note needs a reason.", "reason_required")

    existing = ComplianceDocument.objects.filter(original=original).first()
    if existing is not None:
        raise ComplianceError(
            "That invoice has already been credited.", "already_credited"
        )

    tenant = original.tenant
    number, code = allocate_invoice_number(tenant)
    adapter = adapter_for(mode_for(tenant))

    document = ComplianceDocument.objects.create(
        tenant=tenant,
        sale=original.sale,
        kind=DocumentKind.CREDIT_NOTE,
        original=original,
        invoice_number=number,
        invoice_code=code,
        buyer_pin=original.buyer_pin,
        seller_pin=original.seller_pin,
        # Recorded positive, with the kind carrying the direction. A negative
        # figure on a document that is already called a credit note invites
        # somebody to subtract it twice.
        net_cents=original.net_cents,
        tax_cents=original.tax_cents,
        gross_cents=original.gross_cents,
        tax_breakdown=original.tax_breakdown,
        issued_at=timezone.now(),
        issued_by=user,
        reason=reason,
        adapter=adapter.name,
        submission_state=(
            SubmissionState.PENDING if adapter.submits else SubmissionState.NOT_REQUIRED
        ),
    )

    if not adapter.submits:
        submit(document, adapter)

    record_audit(
        action=AuditAction.REFUND,
        entity=document,
        actor=user,
        request=request,
        tenant_id=tenant.id,
        reason=reason,
        after={"credit_note": code, "credits": original.invoice_code},
    )
    return document


def submit_pending(*, tenant, limit: int = 200) -> dict:
    """Send whatever is waiting.

    Runs outside a checkout, on its own, for the same reason the M-Pesa
    reconciliation job does: a document that could not be sent is a flagged row
    for a person, never a lost sale. A failure leaves the document ``FAILED``
    with the detail recorded, and it is picked up again next time.
    """
    adapter = adapter_for(mode_for(tenant))
    if not adapter.submits:
        return {"attempted": 0, "submitted": 0, "failed": 0}

    waiting = ComplianceDocument.objects.filter(
        tenant=tenant,
        submission_state__in=[SubmissionState.PENDING, SubmissionState.FAILED],
    ).order_by("invoice_number")[:limit]

    submitted = failed = 0
    for document in waiting:
        result = submit(document, adapter)
        if result.succeeded:
            submitted += 1
        else:
            failed += 1

    return {"attempted": submitted + failed, "submitted": submitted, "failed": failed}


def issue_for_settled_sale(*, sale: Sale, buyer_pin: str = "", user=None, request=None):
    """Raise a document for a sale that has just settled, if the shop needs one.

    The hook the checkout view and sync replay both call. Returns None for a
    business under no regime, which is most of them - and doing nothing is the
    correct behaviour there, not a degraded one.
    """
    if mode_for(sale.tenant) == ComplianceMode.NONE and not buyer_pin:
        # Not registered and nobody asked for a tax invoice. Numbering one
        # would put entries in a series the shop does not have.
        return None
    return issue_invoice(sale=sale, buyer_pin=buyer_pin, user=user, request=request)
