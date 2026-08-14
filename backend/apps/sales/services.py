"""
Everything that changes a sale.

Views do not write sales. They call these functions, which means the rules -
what may be edited, what may be voided, when stock moves, when a receipt number
is taken - live in one place rather than being restated at each entry point.
That matters because there will be three entry points before this milestone is
out: the till, the sync endpoint, and an M-Pesa callback.

:func:`recompute_state` is the **single writer** of ``Sale.state``. Nothing else
assigns to it. The state is a cache of the payment and refund ledgers, and one
writer is what keeps the cache from drifting from the rows that justify it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.money import round_cash
from apps.inventory.models import MovementReason, StockItem, apply_movement
from apps.sales.models import (
    Payment,
    PaymentMethod,
    Refund,
    RefundLine,
    RefundMethod,
    Sale,
    SaleDiscrepancy,
    SaleLine,
)
from apps.sales.pricing import LineInput, PricingError, price_cart
from apps.sales.receipts import allocate_receipt_number
from apps.sales.states import (
    LedgerPosition,
    SaleState,
    assert_can_transition,
    derive_state,
)


class CheckoutError(Exception):
    """A sale that cannot proceed, with a message a cashier can act on."""

    def __init__(self, detail: str, code: str = "checkout_error"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass(frozen=True)
class LineRequest:
    """One line as a till asks for it, before prices are looked up."""

    item_id: str
    quantity: Decimal
    unit_price_cents: int | None = None
    discount_bps: int = 0
    discount_cents: int = 0


def ledger_position(sale: Sale) -> LedgerPosition:
    """Sum the two ledgers for one sale.

    Read from the rows every time rather than from any running total kept on the
    sale, because a running total is exactly the thing that could drift from
    what actually happened.
    """
    paid = sale.payments.aggregate(total=Sum("amount_cents"))["total"] or 0
    refunded = sale.refunds.aggregate(total=Sum("amount_cents"))["total"] or 0
    return LedgerPosition(
        total_cents=sale.total_cents,
        paid_cents=paid,
        refunded_cents=refunded,
        rounding_adjustment_cents=sale.rounding_adjustment_cents,
        written_off_cents=sale.offline_shortfall_cents,
    )


def recompute_state(sale: Sale, *, save: bool = True) -> str:
    """Bring the cached state into line with the ledgers.

    The only writer of ``Sale.state``. Also maintains ``is_overpaid``, which is
    not a state but blocks completion in the same way: money taken beyond the
    total is real, and a manager has to give it back before the sale is done
    with.
    """
    position = ledger_position(sale)
    new_state = derive_state(sale.state, position)
    overpaid = position.overpaid_cents > 0

    changed = new_state != sale.state or overpaid != sale.is_overpaid
    sale.state = new_state
    sale.is_overpaid = overpaid

    if save and changed:
        sale.save(update_fields=["state", "is_overpaid", "updated_at"])
    return new_state


@transaction.atomic
def create_sale(
    *,
    tenant,
    store,
    cashier,
    lines: list[LineRequest],
    device=None,
    cart_discount_bps: int = 0,
    cart_discount_cents: int = 0,
    client_uuid=None,
    customer_phone: str = "",
    note: str = "",
) -> Sale:
    """Open a sale and price it.

    Prices come from the catalogue, not from the request, unless the item is
    marked as having a variable price. Trusting a client-supplied price on an
    ordinary item would let anyone with a till sell at whatever they liked.
    """
    from apps.catalog.models import Item

    if not lines:
        raise CheckoutError("A sale needs at least one item.", "empty_sale")

    items = {
        str(item.id): item
        for item in Item.objects.filter(
            tenant=tenant, id__in=[line.item_id for line in lines]
        ).select_related("tax_rate")
    }

    priced_inputs = []
    for request in lines:
        item = items.get(str(request.item_id))
        if item is None:
            raise CheckoutError("That item is not in this catalogue.", "unknown_item")
        if not item.is_sellable:
            raise CheckoutError(
                f"{item.name} is not available for sale right now.", "item_unavailable"
            )
        if request.quantity <= 0:
            raise CheckoutError("A quantity must be more than zero.", "bad_quantity")

        # A variable-price item takes the cashier's figure; everything else
        # takes the catalogue's, whatever the request claims. Trusting a
        # client-supplied price on an ordinary item would let anyone holding a
        # till sell at whatever they liked and pocket the difference.
        if item.is_price_variable and request.unit_price_cents is not None:
            if request.unit_price_cents < 0:
                raise CheckoutError("A price cannot be negative.", "bad_price")

            # price_cents is a floor, not merely a suggestion. "Braiding from
            # KES 500" means the price rises for longer hair; it does not mean a
            # cashier may quietly sell it for fifty.
            #
            # Selling below the marked price is a *discount* - a separate,
            # auditable concept that records who authorised it and why. Allowing
            # it here as well would give the same outcome two routes, only one of
            # which leaves a trail, and the untrailed one would be the one a
            # dishonest cashier used.
            if request.unit_price_cents < item.price_cents:
                raise CheckoutError(
                    f"{item.name} starts at "
                    f"{item.price_cents / 100:.2f}. To sell below that, apply a "
                    "discount so the reduction is recorded.",
                    "below_minimum_price",
                )
            unit_price = request.unit_price_cents
        else:
            unit_price = item.price_cents

        rate = item.tax_rate
        priced_inputs.append(
            LineInput(
                item_id=str(item.id),
                name=item.name,
                sku=item.sku,
                unit=item.unit,
                unit_price_cents=unit_price,
                quantity=request.quantity,
                tax_rate_bps=rate.rate_bps if rate and rate.is_active else 0,
                tax_is_inclusive=rate.is_inclusive if rate and rate.is_active else True,
                discount_bps=request.discount_bps,
                discount_cents=request.discount_cents,
            )
        )

    try:
        totals = price_cart(
            priced_inputs,
            cart_discount_bps=cart_discount_bps,
            cart_discount_cents=cart_discount_cents,
        )
    except PricingError as exc:
        raise CheckoutError(str(exc), "pricing_error") from exc

    sale = Sale.objects.create(
        tenant=tenant,
        store=store,
        cashier=cashier,
        device=device,
        state=SaleState.OPEN,
        subtotal_cents=totals.subtotal_cents,
        discount_cents=totals.discount_cents,
        tax_cents=totals.tax_cents,
        total_cents=totals.total_cents,
        cart_discount_bps=cart_discount_bps,
        cart_discount_cents=cart_discount_cents,
        customer_phone=customer_phone,
        note=note,
        server_received_at=timezone.now(),
        **({"client_uuid": client_uuid} if client_uuid else {}),
    )

    SaleLine.objects.bulk_create(
        [
            SaleLine(
                tenant=tenant,
                sale=sale,
                item_id=priced.line.item_id,
                name=priced.line.name,
                sku=priced.line.sku,
                unit=priced.line.unit,
                unit_price_cents=priced.line.unit_price_cents,
                quantity=priced.line.quantity,
                line_discount_cents=priced.line_discount_cents,
                cart_discount_share_cents=priced.cart_discount_share_cents,
                tax_rate_bps=priced.line.tax_rate_bps,
                tax_is_inclusive=priced.line.tax_is_inclusive,
                net_cents=priced.net_cents,
                tax_cents=priced.tax_cents,
                gross_cents=priced.gross_cents,
                position=index,
            )
            for index, priced in enumerate(totals.lines)
        ]
    )

    return sale


@transaction.atomic
def take_cash(
    *, sale: Sale, tendered_cents: int, user, round_to_shilling: bool = True
) -> Payment:
    """Take a cash payment.

    Goes through exactly the same ledger and state machine as M-Pesa. There is
    no shortcut for cash just because no callback is involved - the moment cash
    had its own path, the two would drift.

    Cash rounds to the shilling because no smaller coin circulates. The
    difference is recorded on the sale so the drawer reconciles exactly rather
    than drifting a few shillings a day.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.state not in (SaleState.OPEN, SaleState.AWAITING_PAYMENT):
        raise CheckoutError(
            f"This sale is {sale.get_state_display().lower()} and cannot take a payment.",
            "sale_not_payable",
        )

    position = ledger_position(sale)
    outstanding = position.outstanding_cents

    if round_to_shilling and sale.rounding_adjustment_cents == 0:
        rounded = round_cash(outstanding)
        adjustment = rounded - outstanding
        if adjustment:
            sale.rounding_adjustment_cents = adjustment
            sale.save(update_fields=["rounding_adjustment_cents", "updated_at"])
            outstanding = rounded

    if tendered_cents < outstanding:
        raise CheckoutError(
            "That is less than the amount due.", "insufficient_tender"
        )

    change = tendered_cents - outstanding

    payment = Payment.objects.create(
        tenant=sale.tenant,
        sale=sale,
        method=PaymentMethod.CASH,
        amount_cents=outstanding,
        tendered_cents=tendered_cents,
        change_cents=change,
        user=user,
    )

    _settle_if_paid(sale, user=user)
    return payment


def _settle_if_paid(sale: Sale, *, user=None) -> None:
    """Finish a sale once its ledger says it is covered.

    Allocating the receipt number and moving stock happen here, inside the
    caller's transaction, so a sale that fails partway takes its number and its
    stock movements with it.
    """
    previous = sale.state
    recompute_state(sale)

    if sale.state != SaleState.PAID or previous == SaleState.PAID:
        return

    if not sale.receipt_number:
        number, code = allocate_receipt_number(sale.tenant)
        sale.receipt_number = number
        sale.receipt_code = code
        sale.save(update_fields=["receipt_number", "receipt_code", "updated_at"])

    _move_stock_for_sale(sale, user=user)

    record_audit(
        action=AuditAction.CREATE,
        entity=sale,
        actor=user,
        tenant_id=sale.tenant_id,
        after={
            "receipt": sale.receipt_code,
            "total_cents": sale.total_cents,
            "state": sale.state,
        },
    )


def _move_stock_for_sale(sale: Sale, *, user=None) -> None:
    """Take the sold quantities off the shelf.

    Stock may go negative, and that is deliberate: the sale has happened and the
    money is in the drawer, so refusing to record it would put the books further
    from the truth than a negative count does. It is surfaced as a discrepancy
    instead, exactly as a manual adjustment that goes negative is surfaced.
    """
    for line in sale.lines.select_related("item"):
        if not line.item.track_stock:
            continue

        stock_item = StockItem.objects.filter(
            tenant=sale.tenant, item=line.item, store=sale.store
        ).first()
        if stock_item is None:
            # Not tracked at this branch. Nothing to decrement, and inventing a
            # stock row here would assert a starting quantity nobody counted.
            continue

        movement = apply_movement(
            stock_item=stock_item,
            delta=-line.quantity,
            reason=MovementReason.SALE,
            user=user,
            ref_type="sales.Sale",
            ref_id=str(sale.pk),
        )

        if movement.balance_after < 0:
            SaleDiscrepancy.objects.create(
                tenant=sale.tenant,
                sale=sale,
                kind=SaleDiscrepancy.Kind.NEGATIVE_STOCK,
                detail=(
                    f"{line.name} went to {movement.balance_after} at "
                    f"{sale.store.code}. The sale is recorded; the count needs "
                    "checking."
                ),
                context={
                    "item": str(line.item_id),
                    "store": sale.store.code,
                    "balance_after": str(movement.balance_after),
                },
            )


@transaction.atomic
def void_sale(*, sale: Sale, user, reason: str) -> Sale:
    """Abandon a sale that has not been paid.

    Refused once anything has settled. The state machine already forbids the
    transition; this repeats the check against the *ledger* because the two
    answer slightly different questions - the machine knows what the cached
    state allows, the ledger knows whether money actually arrived.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if not reason.strip():
        raise CheckoutError("A void needs a reason.", "reason_required")

    assert_can_transition(sale.state, SaleState.VOID, "Refund it instead.")

    position = ledger_position(sale)
    if position.paid_cents > 0:
        raise CheckoutError(
            "Money has already been taken for this sale, so it cannot be "
            "voided. Refund it instead, which keeps a record of the reversal.",
            "sale_already_paid",
        )

    sale.state = SaleState.VOID
    sale.void_reason = reason
    sale.save(update_fields=["state", "void_reason", "updated_at"])

    record_audit(
        action=AuditAction.VOID,
        entity=sale,
        actor=user,
        tenant_id=sale.tenant_id,
        reason=reason,
        before={"state": SaleState.OPEN},
        after={"state": SaleState.VOID},
    )
    return sale


@transaction.atomic
def refund_sale(
    *,
    sale: Sale,
    user,
    reason: str,
    method: str = RefundMethod.CASH,
    lines: list[dict] | None = None,
    amount_cents: int | None = None,
) -> Refund:
    """Give money back, as a new document.

    Never a mutation of the sale. A partial refund followed by another is two
    rows, and the sale's state is the sum - so what was returned, when, by whom
    and why all survive.

    Either name the lines being returned, or give an amount for a
    goodwill-style refund that is not tied to particular goods.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if not reason.strip():
        raise CheckoutError("A refund needs a reason.", "reason_required")

    position = ledger_position(sale)
    if position.paid_cents <= 0:
        raise CheckoutError(
            "Nothing has been paid for this sale, so there is nothing to "
            "refund. Void it instead.",
            "nothing_to_refund",
        )

    line_rows: list[tuple[SaleLine, Decimal, int, bool]] = []
    if lines:
        total = 0
        for entry in lines:
            sale_line = sale.lines.filter(pk=entry["sale_line_id"]).first()
            if sale_line is None:
                raise CheckoutError("That line is not on this sale.", "unknown_line")

            quantity = Decimal(str(entry["quantity"]))
            if quantity <= 0:
                raise CheckoutError("A refund quantity must be more than zero.", "bad_quantity")

            already = (
                sale_line.refund_lines.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            )
            if already + quantity > sale_line.quantity:
                raise CheckoutError(
                    f"Only {sale_line.quantity - already} of {sale_line.name} is "
                    "left to refund.",
                    "over_refund",
                )

            # Refund at the price actually charged for this line, including its
            # share of any discount - refunding the list price would give back
            # more than the customer paid.
            share = int(
                (Decimal(sale_line.gross_cents) * quantity / sale_line.quantity).to_integral_value(
                    rounding="ROUND_HALF_UP"
                )
            )
            total += share
            line_rows.append((sale_line, quantity, share, bool(entry.get("restock", True))))

        refund_amount = total
    elif amount_cents is not None:
        refund_amount = amount_cents
    else:
        raise CheckoutError(
            "Say which lines are being refunded, or an amount.", "nothing_specified"
        )

    if refund_amount <= 0:
        raise CheckoutError("A refund must be more than zero.", "bad_amount")
    if refund_amount > position.refundable_cents:
        raise CheckoutError(
            f"Only {position.refundable_cents} cents can still be refunded on this sale.",
            "over_refund",
        )

    refund = Refund.objects.create(
        tenant=sale.tenant,
        sale=sale,
        method=method,
        amount_cents=refund_amount,
        reason=reason,
        user=user,
        # An M-Pesa refund is recorded now and sent by the shop separately, so
        # it stays visibly unsettled until someone says otherwise.
        is_settled=method != RefundMethod.MPESA_MANUAL,
    )

    for sale_line, quantity, share, restock in line_rows:
        RefundLine.objects.create(
            tenant=sale.tenant,
            refund=refund,
            sale_line=sale_line,
            quantity=quantity,
            amount_cents=share,
            restock=restock,
        )
        if restock and sale_line.item.track_stock:
            stock_item = StockItem.objects.filter(
                tenant=sale.tenant, item=sale_line.item, store=sale.store
            ).first()
            if stock_item is not None:
                apply_movement(
                    stock_item=stock_item,
                    delta=quantity,
                    reason=MovementReason.REFUND,
                    user=user,
                    ref_type="sales.Refund",
                    ref_id=str(refund.pk),
                )

    recompute_state(sale)

    record_audit(
        action=AuditAction.REFUND,
        entity=sale,
        actor=user,
        tenant_id=sale.tenant_id,
        reason=reason,
        before={"state": SaleState.PAID},
        after={
            "state": sale.state,
            "refund_cents": refund_amount,
            "method": method,
        },
    )
    return refund
