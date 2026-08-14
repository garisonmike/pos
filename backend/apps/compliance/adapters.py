"""
The boundary a compliance regime sits behind.

This is the whole point of the milestone. Sales produce documents; what happens
to a document afterwards is regime-specific and lives here. A real eTIMS
integration later is a new class in this file and a new choice in
``ComplianceMode`` - not a change to how a sale is rung up.

**Two implementations from the start, deliberately.** One implementation does
not prove a boundary, it only describes one; an abstraction is real once
something else fits it. And neither of these is a stub: most small dukas are not
VAT-registered, so ``NullAdapter`` is the common case, and a registered business
without a gateway genuinely does type figures into eTIMS Lite by hand, which is
what ``ManualExportAdapter`` produces.

Both are exercised by the same conformance suite, so a third adapter cannot ship
having quietly redefined what any of these methods mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from django.utils import timezone

from apps.compliance.models import ComplianceDocument, SubmissionState


@dataclass(frozen=True)
class ComplianceResult:
    """What an adapter did with a document.

    A result rather than an exception, because failing to submit is an ordinary
    state for this subsystem - the far end is a government service over a
    Kenyan internet connection - and it must never take a sale down with it.
    """

    state: str
    reference: str = ""
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.state in (SubmissionState.SUBMITTED, SubmissionState.NOT_REQUIRED)

    @property
    def should_retry(self) -> bool:
        return self.state == SubmissionState.FAILED


@runtime_checkable
class ComplianceAdapter(Protocol):
    """What every regime must be able to do.

    Deliberately small. Anything a particular regime needs beyond this -
    signatures, device certificates, control codes - belongs inside its own
    implementation, not widened into the interface where every other adapter
    would have to pretend to have it.
    """

    #: Recorded on each document, so a change of regime stays legible in the
    #: history rather than making old rows look like they went somewhere they
    #: did not.
    name: str

    #: Whether documents from this adapter are queued for submission at all.
    submits: bool

    def issue(self, document: ComplianceDocument) -> ComplianceResult:
        """Send an invoice, or record that there is nothing to send."""

    def credit(self, document: ComplianceDocument) -> ComplianceResult:
        """Send a credit note. Its original is on ``document.original``."""

    def status(self, document: ComplianceDocument) -> ComplianceResult:
        """Ask the far end where a document got to.

        For the case that matters later: a submission whose response was lost.
        The manual and null adapters have nothing to ask, so they answer from
        what is already recorded.
        """


class NullAdapter:
    """For a business that is not registered for VAT.

    Not a stub. Most small dukas are in exactly this position, and they still
    need their documents numbered and recorded - a business that registers next
    year should not find a hole where this year was.

    Nothing is submitted, and that is recorded as ``NOT_REQUIRED`` rather than
    left pending, so "nothing to send" is distinguishable from "not sent yet"
    when somebody comes to look.
    """

    name = "null"
    submits = False

    def issue(self, document: ComplianceDocument) -> ComplianceResult:
        return ComplianceResult(state=SubmissionState.NOT_REQUIRED)

    def credit(self, document: ComplianceDocument) -> ComplianceResult:
        return ComplianceResult(state=SubmissionState.NOT_REQUIRED)

    def status(self, document: ComplianceDocument) -> ComplianceResult:
        return ComplianceResult(state=document.submission_state)


class ManualExportAdapter:
    """For a registered business with no gateway.

    Produces exactly what somebody would otherwise read off a pile of receipts
    and type into eTIMS Lite. The submission is a person, so a document is
    marked ``SUBMITTED`` once it has been exported - the export *is* the act.

    It refuses a document with no seller PIN. A tax invoice from an
    unidentified seller is not a tax invoice, and discovering that at the point
    of typing it in - having already given the customer a document that says
    otherwise - is worse than refusing it here.
    """

    name = "manual-export"
    submits = True

    def issue(self, document: ComplianceDocument) -> ComplianceResult:
        if not document.seller_pin:
            return ComplianceResult(
                state=SubmissionState.FAILED,
                detail=(
                    "This business has no tax PIN recorded, so a tax invoice "
                    "cannot be issued under its name. Add it in settings."
                ),
            )
        return ComplianceResult(
            state=SubmissionState.SUBMITTED,
            reference=f"MANUAL-{document.invoice_code}",
        )

    def credit(self, document: ComplianceDocument) -> ComplianceResult:
        return self.issue(document)

    def status(self, document: ComplianceDocument) -> ComplianceResult:
        # There is no far end to ask. What is recorded is the whole truth.
        return ComplianceResult(
            state=document.submission_state,
            reference=document.submission_reference,
        )


#: Every adapter this system knows about, by the mode that selects it.
#:
#: A registry rather than a chain of ifs, so adding eTIMS later is one entry
#: and one choice on ``ComplianceMode``.
_ADAPTERS: dict[str, ComplianceAdapter] = {
    "NONE": NullAdapter(),
    "MANUAL": ManualExportAdapter(),
}


def adapter_for(mode: str) -> ComplianceAdapter:
    """The adapter a business's documents go through.

    An unknown mode falls back to the null adapter rather than raising. A
    setting that has drifted - a regime withdrawn, a value hand-edited - must
    not stop a shop selling; it should stop it *submitting*, which is exactly
    what the null adapter does.
    """
    return _ADAPTERS.get(mode, _ADAPTERS["NONE"])


def submit(document: ComplianceDocument, adapter: ComplianceAdapter) -> ComplianceResult:
    """Run a document through its adapter and record the outcome.

    The only place that writes submission state. Everything it touches is in
    ``ComplianceDocument.MUTABLE_AFTER_ISSUE`` - a submission that succeeds on
    the third attempt has not changed a single tax fact.
    """
    result = (
        adapter.credit(document) if document.is_credit_note else adapter.issue(document)
    )

    document.submission_state = result.state
    document.submission_reference = result.reference
    document.failure_detail = result.detail
    document.submission_attempts += 1
    if result.state == SubmissionState.SUBMITTED:
        document.submitted_at = timezone.now()

    document.save(
        update_fields=[
            "submission_state",
            "submission_reference",
            "failure_detail",
            "submission_attempts",
            "submitted_at",
            "updated_at",
        ]
    )
    return result
