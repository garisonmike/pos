"""
The tax document a sale produces, and the counter that numbers it.

This milestone is not a KRA integration. It is a **boundary**. Every sale starts
carrying what any compliance regime needs, and the regime-specific part sits
behind an adapter, so that a real eTIMS integration later is a new adapter
rather than a rewrite of the sales code.

Three decisions follow from that.

**An invoice number is not a receipt number.** A receipt number identifies what
a customer was handed. An invoice number identifies a *taxable document*, and
the two diverge immediately: a void never gets an invoice number, a credit note
gets one of its own, and a business not registered for VAT gets receipt numbers
and no invoices at all. Sharing one series would put gaps in the tax sequence
the moment anything was voided - and a gapless tax sequence is precisely what a
revenue authority looks at.

**A compliance document is immutable once issued.** A correction is a credit
note referencing the original, never an edit. That is the same discipline the
payment and refund ledgers follow, and here it is not only principle: an
editable tax record is not a tax record.

**The tax breakdown is snapshotted, not derived on read.** A rate change next
year must not retroactively restate what was declared.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel

#: A Kenyan tax PIN: a letter, nine digits, a letter. ``P051234567X``.
#:
#: Shape only. Nothing here claims the PIN is *valid* - that cannot be known
#: without asking KRA, and a field that looks verified and is not is worse than
#: one that plainly is not.
KRA_PIN_PATTERN = re.compile(r"^[A-Z]\d{9}[A-Z]$")


def looks_like_a_kra_pin(value: str) -> bool:
    """Whether a string has the shape of a tax PIN.

    Deliberately named for what it does. ``is_valid_pin`` would be a lie.
    """
    return bool(KRA_PIN_PATTERN.match((value or "").strip().upper()))


def validate_kra_pin(value: str) -> None:
    if value and not looks_like_a_kra_pin(value):
        raise ValidationError(
            "A tax PIN looks like P051234567X - a letter, nine digits, a letter."
        )


class ComplianceMode(models.TextChoices):
    """Which adapter a business's documents go through.

    Defaults to ``NONE``, because most small dukas are not VAT-registered. That
    is not a stub for the common case - it *is* the common case.
    """

    NONE = "NONE", "Not registered for VAT"
    MANUAL = "MANUAL", "Recorded here, entered into eTIMS by hand"


class DocumentKind(models.TextChoices):
    INVOICE = "INVOICE", "Tax invoice"
    CREDIT_NOTE = "CREDIT_NOTE", "Credit note"


class SubmissionState(models.TextChoices):
    """Where a document has got to on its way out of this system.

    ``NOT_REQUIRED`` is a real state rather than a null: a business that is not
    registered still gets its documents recorded, and "nothing to submit" must
    be distinguishable from "not submitted yet" when somebody comes to look.
    """

    NOT_REQUIRED = "NOT_REQUIRED", "Nothing to submit"
    PENDING = "PENDING", "Waiting to be sent"
    SUBMITTED = "SUBMITTED", "Sent"
    FAILED = "FAILED", "Could not be sent"


class InvoiceCounter(TenantOwnedModel, TimeStampedModel):
    """The last invoice number issued to a business.

    One row per business, locked for the duration of each allocation - the same
    shape as ``ReceiptCounter``, which already works and is already proved
    against concurrency. Kept as a separate counter rather than a second column
    on that one, because the two series advance at different rates and a shared
    row would serialise every sale behind every invoice.
    """

    last_number = models.BigIntegerField(default=0)
    prefix = models.CharField(
        max_length=12,
        default="TI",
        help_text="Printed before the number, e.g. TI-2026-000001. 'Tax invoice'.",
    )

    class Meta:
        db_table = "compliance_invoice_counter"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"], name="one_invoice_counter_per_tenant"
            )
        ]

    def __str__(self) -> str:
        return f"{self.prefix}: {self.last_number}"


class ComplianceDocument(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """One taxable document. Immutable once issued.

    Carries a frozen snapshot of the figures rather than reading them through
    the sale, so that a price or rate change later cannot restate what was
    declared. The sale is still referenced, because a person looking at a
    document needs to be able to get back to what was sold.
    """

    sale = models.ForeignKey(
        "sales.Sale",
        on_delete=models.PROTECT,
        related_name="compliance_documents",
        help_text="PROTECT: a sale with a tax document against it is not deletable.",
    )

    kind = models.CharField(max_length=12, choices=DocumentKind.choices)

    invoice_number = models.BigIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Gapless per business, allocated inside the sale's own "
            "transaction. **Null when nothing will be filed against this "
            "document** - a business under no regime, or an offline sale that "
            "has not synced. Putting either in the tax series would claim a "
            "filing that will never happen. Postgres treats nulls as distinct, "
            "so the unique constraint still holds."
        ),
    )
    invoice_code = models.CharField(
        max_length=40,
        blank=True,
        help_text="The number as it is printed, e.g. TI-2026-000001. Empty when unnumbered.",
    )

    #: The credit note's original. Null on an invoice.
    original = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="credit_notes",
        null=True,
        blank=True,
    )

    # ---- Frozen at issue -------------------------------------------------

    buyer_pin = models.CharField(
        max_length=20,
        blank=True,
        validators=[validate_kra_pin],
        help_text=(
            "The customer's tax PIN, when they asked for a tax invoice. Checked "
            "for shape only - nothing here can verify it without asking KRA."
        ),
    )
    seller_pin = models.CharField(
        max_length=20,
        blank=True,
        help_text="The business's own PIN, copied at issue so a later edit cannot rewrite it.",
    )

    net_cents = models.BigIntegerField()
    tax_cents = models.BigIntegerField()
    gross_cents = models.BigIntegerField()

    tax_breakdown = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Net and tax per rate, as a list of {rate_bps, net_cents, "
            "tax_cents}. Snapshotted rather than derived on read, so a rate "
            "change next year cannot restate what was declared."
        ),
    )

    issued_at = models.DateTimeField()
    issued_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        related_name="issued_documents",
        null=True,
        blank=True,
    )
    reason = models.TextField(
        blank=True, help_text="Why a credit note was raised. Required on one."
    )

    # ---- Submission ------------------------------------------------------

    adapter = models.CharField(
        max_length=32,
        help_text="Which adapter handled it, recorded so a change of regime stays legible.",
    )
    submission_state = models.CharField(
        max_length=14,
        choices=SubmissionState.choices,
        default=SubmissionState.PENDING,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submission_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text="Whatever the far end called it. Empty for the manual adapter.",
    )
    submission_attempts = models.PositiveIntegerField(default=0)
    failure_detail = models.TextField(blank=True)

    class Meta:
        db_table = "compliance_document"
        ordering = ("-issued_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "invoice_number"],
                name="unique_invoice_number_per_tenant",
            ),
            # A credit note without an original is not a correction of
            # anything, and an invoice that points at one is a contradiction.
            models.CheckConstraint(
                condition=(
                    models.Q(kind="CREDIT_NOTE", original__isnull=False)
                    | models.Q(kind="INVOICE", original__isnull=True)
                ),
                name="credit_note_has_an_original",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "submission_state"]),
            models.Index(fields=["tenant", "-issued_at"]),
        ]

    def __str__(self) -> str:
        return self.invoice_code or f"unnumbered {self.kind.lower()} on {self.sale_id}"

    @property
    def is_numbered(self) -> bool:
        """Whether this document is part of the business's tax series.

        An unnumbered one is a record that a customer asked for a tax invoice
        from a shop that is not registered. Worth keeping; not worth filing.
        """
        return self.invoice_number is not None

    @property
    def is_credit_note(self) -> bool:
        return self.kind == DocumentKind.CREDIT_NOTE

    @property
    def needs_submission(self) -> bool:
        return self.submission_state in (
            SubmissionState.PENDING,
            SubmissionState.FAILED,
        )

    #: The fields a document may change after it is issued.
    #:
    #: Everything else is frozen. These are all about the *journey out* of this
    #: system, not about what was declared - a submission that succeeds on the
    #: third attempt has not changed the tax facts.
    MUTABLE_AFTER_ISSUE = frozenset(
        {
            "submission_state",
            "submitted_at",
            "submission_reference",
            "submission_attempts",
            "failure_detail",
            "updated_at",
        }
    )

    def save(self, *args, **kwargs):
        """Refuse to rewrite a document that has already been issued.

        Enforced here rather than left to discipline at call sites, because the
        call sites are the thing most likely to change. An update that touches
        only submission bookkeeping is allowed through; anything touching the
        figures, the number or the parties is not.
        """
        if self.pk is not None and not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                raise ValueError(
                    "A compliance document is immutable once issued. A "
                    "correction is a credit note referencing it, never an edit."
                )
            touched = set(update_fields) - self.MUTABLE_AFTER_ISSUE
            if touched:
                raise ValueError(
                    "A compliance document is immutable once issued. "
                    f"Refusing to change {sorted(touched)}. A correction is a "
                    "credit note referencing it, never an edit."
                )
        return super().save(*args, **kwargs)
