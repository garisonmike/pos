"""
Starting an M-Pesa payment, and applying one.

:func:`settle_intent` is the **single guarded path by which M-Pesa money is
applied to a sale**. Both the callback and the reconciliation job go through it.
That is deliberate rather than tidy: two implementations of "is this sale still
creditable" would eventually disagree, and the disagreement would show up as a
customer charged twice or a sale marked paid that was not.

The guard reads ground truth, in the same way ``void_sale`` does. Whether money
has arrived is answered by summing the ledger, never by trusting the cached
state column - so a cached state that has gone wrong cannot let a payment
through.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.payments.daraja import DarajaError, get_client
from apps.payments.models import (
    CallbackOutcome,
    IntentState,
    MpesaCredential,
    PaymentIntent,
    SuspectReason,
)
from apps.sales.models import (
    Payment,
    PaymentConfirmation,
    PaymentMethod,
    Sale,
    SaleDiscrepancy,
)
from apps.sales.services import CheckoutError, ledger_position, recompute_state
from apps.sales.states import SaleState

#: Marks a payment reference that came from a status query rather than a
#: callback. Visibly not an M-Pesa receipt code, which are alphanumeric and
#: never contain a hyphen. Defined here rather than in reconciliation.py so that
#: the backfill below can recognise one without importing that module - which
#: already imports this one.
RECONCILED_PREFIX = "RECON-"


class StkError(Exception):
    """A push that could not be started."""

    def __init__(self, detail: str, code: str = "stk_error", status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status


@dataclass
class SettlementOutcome:
    """What happened when a payment was offered to a sale."""

    credited: bool
    outcome: str
    payment: Payment | None = None
    suspect_reason: str = ""
    detail: str = ""
    #: A placeholder reference was replaced with a real M-Pesa receipt code.
    #: Not a credit - no money moved - but the payment row is now reconcilable
    #: against the business's own M-Pesa statement.
    backfilled: bool = False


def callback_url_for(intent: PaymentIntent) -> str:
    """Where Safaricom should post this intent's result.

    The token in the path is what lets an unauthenticated callback find its
    business and its sale, so each intent gets its own.
    """
    base = getattr(settings, "MPESA_CALLBACK_BASE_URL", "").rstrip("/")
    return f"{base}/api/v1/payments/mpesa/callback/{intent.callback_token}/"


# ---------------------------------------------------------------------------
# Starting a payment
# ---------------------------------------------------------------------------


@transaction.atomic
def initiate_stk(*, sale: Sale, phone: str, user, client_uuid=None) -> PaymentIntent:
    """Send a payment prompt to a customer's phone.

    Refuses while another prompt is still live on the same sale. That refusal is
    what keeps a genuine double charge rare rather than merely survivable: two
    prompts answered by an obliging customer are two real payments, and no
    amount of idempotency downstream can un-take money that was actually sent.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.state not in (SaleState.OPEN, SaleState.AWAITING_PAYMENT):
        raise StkError(
            f"This sale is {sale.get_state_display().lower()} and cannot take a payment.",
            "sale_not_payable",
        )

    # The till retrying its own request must not produce a second prompt.
    if client_uuid:
        existing = PaymentIntent.objects.filter(
            tenant=sale.tenant, client_uuid=client_uuid
        ).first()
        if existing is not None:
            return existing

    live = (
        PaymentIntent.objects.select_for_update()
        .filter(sale=sale, state=IntentState.PENDING, expires_at__gt=timezone.now())
        .first()
    )
    if live is not None:
        raise StkError(
            "A payment prompt is already waiting on this sale. Let it finish or "
            "time out before sending another - two prompts can take the money "
            "twice.",
            "push_already_pending",
            status=409,
        )

    credential = MpesaCredential.objects.filter(tenant=sale.tenant, is_active=True).first()
    if credential is None:
        raise StkError(
            "M-Pesa is not set up for this business yet.", "mpesa_not_configured"
        )

    # Cash rounding is folded into the position itself, so this is simply what
    # the customer still owes. It used to add the adjustment back by hand here,
    # which was a local patch over ledger_position not knowing about it - and
    # every other caller was quietly getting the wrong figure.
    outstanding = ledger_position(sale).outstanding_cents
    if outstanding <= 0:
        raise StkError("There is nothing left to pay on this sale.", "nothing_due")

    intent = PaymentIntent.objects.create(
        tenant=sale.tenant,
        sale=sale,
        amount_cents=outstanding,
        phone=phone,
        callback_token=PaymentIntent.new_callback_token(),
        expires_at=timezone.now()
        + timezone.timedelta(seconds=getattr(settings, "MPESA_INTENT_TIMEOUT_SECONDS", 90)),
        initiated_by=user,
        **({"client_uuid": client_uuid} if client_uuid else {}),
    )

    try:
        result = get_client(credential).stk_push(
            amount_cents=outstanding,
            phone=phone,
            reference=sale.receipt_code or str(sale.pk)[:12],
            description=f"Sale {sale.pk.hex[:8]}",
            callback_url=callback_url_for(intent),
        )
    except DarajaError as exc:
        intent.state = IntentState.FAILED
        intent.result_description = exc.detail
        intent.completed_at = timezone.now()
        intent.save(update_fields=["state", "result_description", "completed_at", "updated_at"])
        raise StkError(exc.detail, exc.code) from exc

    intent.checkout_request_id = result.checkout_request_id
    intent.merchant_request_id = result.merchant_request_id
    intent.save(
        update_fields=["checkout_request_id", "merchant_request_id", "updated_at"]
    )

    # The sale is now waiting on a customer, which is a state fact rather than a
    # ledger one - no row records "a prompt was sent".
    if sale.state != SaleState.AWAITING_PAYMENT:
        sale.state = SaleState.AWAITING_PAYMENT
        sale.save(update_fields=["state", "updated_at"])

    return intent


# ---------------------------------------------------------------------------
# Applying a payment
# ---------------------------------------------------------------------------


def creditable_refusal(sale: Sale) -> str:
    """Why this sale may not be credited, or an empty string if it may.

    Two kinds of fact, from two sources, and the split is the point.

    *State facts* - void, and whether a prompt is outstanding - have no ledger
    row to derive them from, so they are read from the state column.

    *Money facts* - has this been paid, has it been refunded - are summed from
    the ledger every time, never taken from the cached state. That is what makes
    the guard hold when the cache is wrong: a sale whose column says
    AWAITING_PAYMENT but whose payments already cover it is refused, because the
    money is what decides.
    """
    if sale.state == SaleState.VOID:
        return SuspectReason.SALE_NOT_AWAITING

    if sale.state != SaleState.AWAITING_PAYMENT:
        # Includes OPEN, which is where a lapsed prompt returns a sale. A late
        # success there is genuine money, and is held for a person rather than
        # applied to a sale that may since have been re-rung or paid in cash.
        return SuspectReason.SALE_NOT_AWAITING

    position = ledger_position(sale)
    if position.is_settled or position.refunded_cents > 0:
        return SuspectReason.SALE_NOT_AWAITING

    return ""


@transaction.atomic
def settle_intent(
    *,
    intent: PaymentIntent,
    mpesa_receipt_number: str,
    amount_cents: int,
    phone: str,
    source: str,
    user=None,
) -> SettlementOutcome:
    """Apply an M-Pesa payment to a sale, or explain why it was not applied.

    The one place this happens. The callback calls it; so does the
    reconciliation job. Both take the same locks in the same order, so the two
    racing on one intent cannot both credit it - the loser finds the intent
    already resolved or the receipt already recorded.
    """
    intent = PaymentIntent.objects.select_for_update().get(pk=intent.pk)
    sale = Sale.objects.select_for_update().get(pk=intent.sale_id)

    # Idempotency key four: this exact movement of money, whatever the path.
    already = Payment.objects.filter(
        tenant=intent.tenant, mpesa_receipt_number=mpesa_receipt_number
    ).first()
    if mpesa_receipt_number and already is not None:
        return SettlementOutcome(
            credited=False,
            outcome=CallbackOutcome.DUPLICATE,
            payment=already,
            detail="This M-Pesa receipt has already been credited.",
        )

    if intent.state != IntentState.PENDING:
        # A late callback for an attempt already settled by the reconciliation
        # job carries the one thing that job could not get: the real M-Pesa
        # receipt code. Take it.
        backfilled = backfill_reconciled_receipt(
            intent=intent, mpesa_receipt_number=mpesa_receipt_number
        )
        return SettlementOutcome(
            credited=False,
            backfilled=backfilled,
            outcome=CallbackOutcome.SUSPECT,
            suspect_reason=SuspectReason.INTENT_ALREADY_SETTLED,
            detail=f"This payment attempt was already {intent.get_state_display().lower()}.",
        )

    refusal = creditable_refusal(sale)
    if refusal:
        _raise_late_payment_discrepancy(
            sale=sale, intent=intent, amount_cents=amount_cents, receipt=mpesa_receipt_number
        )
        return SettlementOutcome(
            credited=False,
            outcome=CallbackOutcome.SUSPECT,
            suspect_reason=refusal,
            detail=(
                "This sale is no longer waiting for payment, so the money has "
                "not been applied. Someone needs to look at it."
            ),
        )

    if amount_cents != intent.amount_cents:
        return SettlementOutcome(
            credited=False,
            outcome=CallbackOutcome.SUSPECT,
            suspect_reason=SuspectReason.AMOUNT_MISMATCH,
            detail=(
                f"Expected {intent.amount_cents} cents but the payment was "
                f"{amount_cents}."
            ),
        )

    try:
        payment = Payment.objects.create(
            tenant=intent.tenant,
            sale=sale,
            method=PaymentMethod.MPESA,
            amount_cents=amount_cents,
            mpesa_receipt_number=mpesa_receipt_number,
            mpesa_phone=phone,
            confirmed_via=(
                PaymentConfirmation.RECONCILIATION
                if source == "RECONCILIATION"
                else PaymentConfirmation.CALLBACK
            ),
            intent=intent,
            user=user,
        )
    except IntegrityError:
        # The unique constraint on the receipt number, reached by two paths at
        # once. The other one won; this is not an error worth surfacing.
        return SettlementOutcome(
            credited=False,
            outcome=CallbackOutcome.DUPLICATE,
            detail="This M-Pesa receipt has already been credited.",
        )

    intent.state = IntentState.SUCCEEDED
    intent.result_code = 0
    intent.completed_at = timezone.now()
    if source == "RECONCILIATION":
        intent.reconciled_at = timezone.now()
    intent.save(
        update_fields=[
            "state",
            "result_code",
            "completed_at",
            "reconciled_at",
            "updated_at",
        ]
    )

    from apps.sales.services import _settle_if_paid

    _settle_if_paid(sale, user=user)

    record_audit(
        action=AuditAction.CREATE,
        entity=payment,
        actor=user,
        tenant_id=sale.tenant_id,
        reason=f"M-Pesa payment applied via {source.lower()}",
        after={
            "amount_cents": amount_cents,
            "mpesa_receipt_number": mpesa_receipt_number,
            "sale": str(sale.pk),
            "source": source,
        },
    )

    return SettlementOutcome(
        credited=True, outcome=CallbackOutcome.APPLIED, payment=payment
    )


def backfill_reconciled_receipt(*, intent: PaymentIntent, mpesa_receipt_number: str) -> bool:
    """Replace a placeholder reference with the real M-Pesa receipt code.

    The one place a ``Payment`` row is edited after it is written, and worth
    justifying since every other row here is append-only.

    When the reconciliation job settles an attempt, it can only prove *that* the
    customer paid - Daraja's status query does not return the receipt code. The
    payment therefore carries ``RECON-<checkout request id>``, which keeps the
    unique constraint working but is useless for matching against the business's
    own M-Pesa statement. If the missing callback later turns up, it carries the
    real code, and this is the only moment that code ever becomes available.

    Nothing about the money changes: same amount, same sale, same timestamp,
    same intent. A placeholder is replaced by the fact it stood in for. The sale
    is not re-credited and its state is not touched - the callback is still
    recorded as suspect, because a payment arriving after settlement is worth a
    person's attention regardless.

    Returns whether anything was replaced.
    """
    if not mpesa_receipt_number or mpesa_receipt_number.startswith(RECONCILED_PREFIX):
        return False

    payment = (
        Payment.objects.select_for_update()
        .filter(intent=intent, confirmed_via=PaymentConfirmation.RECONCILIATION)
        .first()
    )
    if payment is None:
        return False

    previous = payment.mpesa_receipt_number
    payment.mpesa_receipt_number = mpesa_receipt_number
    payment.confirmed_via = PaymentConfirmation.CALLBACK

    try:
        with transaction.atomic():
            payment.save(
                update_fields=["mpesa_receipt_number", "confirmed_via", "updated_at"]
            )
    except IntegrityError:
        # That receipt code is already on another payment in this business, so
        # it has been credited elsewhere. Leaving the placeholder alone is the
        # safe answer; the callback stays suspect and a person can work out
        # which sale the money really belongs to.
        return False

    record_audit(
        action=AuditAction.UPDATE,
        entity=payment,
        tenant_id=payment.tenant_id,
        reason="Late callback supplied the M-Pesa receipt for a reconciled payment",
        before={"mpesa_receipt_number": previous, "confirmed_via": "RECONCILIATION"},
        after={
            "mpesa_receipt_number": mpesa_receipt_number,
            "confirmed_via": "CALLBACK",
        },
    )
    return True


def _raise_late_payment_discrepancy(*, sale, intent, amount_cents, receipt) -> None:
    """Record money that arrived for a sale that had moved on.

    The sale is not touched. This is the row a manager reads to find out that
    the shop is holding money it has not applied to anything.
    """
    SaleDiscrepancy.objects.create(
        tenant=sale.tenant,
        sale=sale,
        kind=SaleDiscrepancy.Kind.LATE_PAYMENT,
        detail=(
            f"An M-Pesa payment of {amount_cents} cents arrived for a sale that "
            f"is {sale.get_state_display().lower()}. It has not been applied."
        ),
        context={
            "intent": str(intent.pk),
            "mpesa_receipt_number": receipt,
            "amount_cents": amount_cents,
            "sale_state": sale.state,
        },
    )


def mark_intent_failed(*, intent: PaymentIntent, result_code: int, description: str) -> None:
    """Record a push the customer refused, cancelled or ignored.

    The sale goes back to being an open cart so the cashier can try again or
    take cash, which is the only useful thing to do with a refused prompt.
    """
    intent.state = IntentState.FAILED
    intent.result_code = result_code
    intent.result_description = description
    intent.completed_at = timezone.now()
    intent.save(
        update_fields=[
            "state",
            "result_code",
            "result_description",
            "completed_at",
            "updated_at",
        ]
    )

    sale = Sale.objects.select_for_update().get(pk=intent.sale_id)
    if sale.state == SaleState.AWAITING_PAYMENT and not ledger_position(sale).paid_cents:
        sale.state = SaleState.OPEN
        sale.save(update_fields=["state", "updated_at"])


__all__ = [
    "CheckoutError",
    "SettlementOutcome",
    "StkError",
    "callback_url_for",
    "creditable_refusal",
    "initiate_stk",
    "mark_intent_failed",
    "recompute_state",
    "settle_intent",
]
