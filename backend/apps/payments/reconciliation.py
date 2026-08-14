"""
Chasing payments whose callback never arrived.

A lost callback is at least as likely as a duplicated one, and no amount of
idempotency on the receiving side helps with a message that never came. Without
this, a customer who paid would be told they had not, and the shop would be
holding money it had not applied to anything.

**It goes through the same guarded path the callback uses.** ``settle_intent``
takes the locks, checks the same ground-truth guard and writes the same rows.
Two implementations of "is this sale still creditable" would eventually
disagree, and the disagreement would show up as a customer charged twice.

**It cannot double-credit against a callback landing at the same moment.** Both
paths take ``select_for_update`` on the intent before doing anything, so one
waits for the other. Whichever loses finds the intent no longer ``PENDING`` and
returns without writing. Underneath that, the unique constraint on the M-Pesa
receipt number is the backstop for any path at all.

**On the receipt number.** Daraja's status query confirms *whether* a payment
succeeded but does not return the M-Pesa receipt code - only the callback
carries that. Two bad options and one acceptable one:

* Refuse to credit without a receipt: a customer who paid is told they have not,
  and every lost callback becomes manual work. Rejected.
* Credit with a blank reference: the unique constraint excludes blanks, so the
  one guard that catches a double credit by any path stops working. Rejected.
* Credit with the checkout request id under a visible prefix, which is unique
  per attempt so the guard still holds, and is obviously not an M-Pesa code so
  nobody mistakes it for one. Chosen.

A real callback arriving afterwards finds the intent already settled, so it is
not credited again - but it carries the receipt code this job could not get, and
that code is backfilled onto the existing payment. That is the only moment the
real code ever becomes available, and without it a reconciled sale could never
be matched against the business's own M-Pesa statement. The callback is still
recorded as suspect: a payment arriving after settlement is worth a person's
attention regardless. See ``backfill_reconciled_receipt``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.tenancy import bypass_rls, tenant_context
from apps.payments.daraja import DarajaError, get_client
from apps.payments.models import IntentState, MpesaCredential, PaymentIntent
from apps.payments.services import (
    RECONCILED_PREFIX,
    mark_intent_failed,
    settle_intent,
)


@dataclass
class ReconciliationReport:
    """What one run did."""

    examined: int = 0
    credited: int = 0
    failed: int = 0
    still_pending: int = 0
    unreachable: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "examined": self.examined,
            "credited": self.credited,
            "failed": self.failed,
            "still_pending": self.still_pending,
            "unreachable": self.unreachable,
            "skipped": self.skipped,
            "notes": self.notes,
        }


def lapsed_intents(*, grace_seconds: int | None = None):
    """Payment attempts old enough that a callback should have arrived.

    The grace period is on top of the prompt's own expiry, so an intent is only
    chased once the customer's window has closed *and* a little longer - chasing
    one while the customer is still typing their PIN would ask Daraja a question
    it cannot yet answer.

    Read under bypass because this runs from a command with no request and no
    business bound. Each intent is then handled inside its own tenant context.
    """
    grace = (
        grace_seconds
        if grace_seconds is not None
        else getattr(settings, "MPESA_RECONCILE_GRACE_SECONDS", 120)
    )
    cutoff = timezone.now() - timezone.timedelta(seconds=grace)

    with transaction.atomic(), bypass_rls():
        return list(
            PaymentIntent.all_objects.filter(
                state=IntentState.PENDING, expires_at__lt=cutoff
            ).select_related("tenant", "sale")
        )


def reconcile_intent(intent: PaymentIntent, *, report: ReconciliationReport) -> None:
    """Ask Daraja what happened to one attempt, and act on the answer."""
    with transaction.atomic(), tenant_context(intent.tenant_id):
        credential = MpesaCredential.objects.filter(
            tenant_id=intent.tenant_id, is_active=True
        ).first()
        if credential is None:
            report.skipped += 1
            report.notes.append(f"{intent.pk}: no M-Pesa credentials on this business")
            return

        try:
            result = get_client(credential).stk_query(
                checkout_request_id=intent.checkout_request_id
            )
        except DarajaError as exc:
            # Left pending on purpose. An unreachable Daraja is not evidence of
            # anything, and marking the attempt failed would tell a customer who
            # paid that they had not.
            report.unreachable += 1
            report.notes.append(f"{intent.pk}: {exc.detail}")
            return

        if result.still_pending:
            report.still_pending += 1
            return

        if result.succeeded:
            outcome = settle_intent(
                intent=intent,
                mpesa_receipt_number=f"{RECONCILED_PREFIX}{intent.checkout_request_id}",
                amount_cents=intent.amount_cents,
                phone=intent.phone,
                source="RECONCILIATION",
            )
            if outcome.credited:
                report.credited += 1
                report.notes.append(
                    f"{intent.pk}: credited {intent.amount_cents} cents from a status query"
                )
            else:
                report.skipped += 1
                report.notes.append(f"{intent.pk}: {outcome.detail or outcome.suspect_reason}")
            return

        mark_intent_failed(
            intent=intent,
            result_code=result.result_code,
            description=result.result_description or "Confirmed unpaid by status query",
        )
        report.failed += 1


def reconcile(*, grace_seconds: int | None = None) -> ReconciliationReport:
    """Chase every attempt that has gone quiet."""
    report = ReconciliationReport()

    for intent in lapsed_intents(grace_seconds=grace_seconds):
        report.examined += 1
        reconcile_intent(intent, report=report)

    return report
