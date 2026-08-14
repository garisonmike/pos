"""
Opening a drawer, and counting it at the end.

The arithmetic is small. What matters is the order things happen in, and what
is never allowed to happen at all.

``close_shift`` is the only writer of ``expected_closing_cents`` and
``variance_cents``, and it writes them exactly once. Nothing recomputes them
afterwards, which is the whole point: a figure a person put their name to has
to mean the same thing next week as it did the day they signed it.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.constants import UserRole
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.sales.models import Payment, PaymentMethod, RefundMethod
from apps.shifts.models import (
    CashMovement,
    CashMovementKind,
    DrawerCount,
    Shift,
    ShiftDiscrepancy,
    ShiftState,
)


class ShiftError(Exception):
    """Something a cashier can act on, rather than a bug."""

    def __init__(self, detail: str, code: str = "shift_error"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass(frozen=True)
class DrawerPosition:
    """What the drawer should hold, and how it got there.

    Every part is kept separately rather than reduced to one figure, because a
    cashier who is nine hundred short needs to see which line the money should
    have come from, not just that it is missing.
    """

    opening_float_cents: int
    cash_sales_cents: int
    cash_refunds_cents: int
    paid_in_cents: int
    paid_out_cents: int

    @property
    def expected_cents(self) -> int:
        return (
            self.opening_float_cents
            + self.cash_sales_cents
            - self.cash_refunds_cents
            + self.paid_in_cents
            - self.paid_out_cents
        )

    def as_dict(self) -> dict:
        return {
            "opening_float_cents": self.opening_float_cents,
            "cash_sales_cents": self.cash_sales_cents,
            "cash_refunds_cents": self.cash_refunds_cents,
            "paid_in_cents": self.paid_in_cents,
            "paid_out_cents": self.paid_out_cents,
            "expected_cents": self.expected_cents,
        }


def drawer_position(shift: Shift) -> DrawerPosition:
    """Sum what should be in the drawer, from the rows that justify it.

    **Cash only.** An M-Pesa payment never touched this drawer, and folding
    mobile money into the expectation is how a till reads twenty thousand short
    every day until nobody trusts the reconciliation at all.
    """
    cash_sales = (
        shift.payments.filter(method=PaymentMethod.CASH).aggregate(
            total=Sum("amount_cents")
        )["total"]
        or 0
    )

    # Refunds given back in cash come out of the same drawer. An M-Pesa refund
    # is settled outside it and is excluded for the same reason its payment is.
    refunds = 0
    for payment in shift.payments.filter(method=PaymentMethod.CASH).select_related("sale"):
        refunds += (
            payment.sale.refunds.filter(method=RefundMethod.CASH).aggregate(
                total=Sum("amount_cents")
            )["total"]
            or 0
        )

    movements = shift.movements.all()
    paid_in = sum(m.amount_cents for m in movements if m.kind == CashMovementKind.PAID_IN)
    paid_out = sum(
        m.amount_cents for m in movements if m.kind != CashMovementKind.PAID_IN
    )

    return DrawerPosition(
        opening_float_cents=shift.opening_float_cents,
        cash_sales_cents=cash_sales,
        cash_refunds_cents=refunds,
        paid_in_cents=paid_in,
        paid_out_cents=paid_out,
    )


def open_shift_for(user) -> Shift | None:
    """The drawer this person is currently accountable for, if any.

    Returns None for a business that does not run shifts. Shifts are optional -
    a duka with one person and one drawer may never open one, and it must go on
    selling exactly as before.
    """
    return Shift.objects.filter(cashier=user, state=ShiftState.OPEN).first()


@transaction.atomic
def open_shift(
    *,
    tenant,
    store,
    cashier,
    opening_float_cents: int,
    device=None,
    client_uuid=None,
    note: str = "",
    request=None,
) -> Shift:
    """Start a drawer.

    Refuses a second open drawer for the same person. Two would make "which
    drawer did this sale go into" unanswerable, and that answer is the entire
    reason the record exists.
    """
    if opening_float_cents < 0:
        raise ShiftError("An opening float cannot be negative.", "negative_float")

    existing = open_shift_for(cashier)
    if existing is not None:
        raise ShiftError(
            "This person already has a drawer open. Close it before starting another.",
            "shift_already_open",
        )

    shift = Shift.objects.create(
        tenant=tenant,
        store=store,
        device=device,
        cashier=cashier,
        state=ShiftState.OPEN,
        opened_at=timezone.now(),
        opening_float_cents=opening_float_cents,
        opening_note=note,
        **({"client_uuid": client_uuid} if client_uuid else {}),
    )

    record_audit(
        action=AuditAction.SHIFT_OPENED,
        entity=shift,
        actor=cashier,
        request=request,
        reason=note,
        after={
            "opening_float_cents": opening_float_cents,
            "store": store.code,
            "cashier": cashier.username,
        },
    )
    return shift


@transaction.atomic
def close_shift(
    *,
    shift: Shift,
    declared_closing_cents: int,
    closed_by,
    note: str = "",
    denominations: dict[int, int] | None = None,
    request=None,
) -> Shift:
    """Count the drawer and finish the shift.

    The declared figure arrives from the caller, who has not been told the
    expectation - see ``ShiftViewSet``. This function computes the expectation
    only after the count is in hand, which is what makes the blindness real
    rather than a matter of the interface politely not showing it.

    A variance never blocks the close. The count is a fact to be recorded, and
    a shop that cannot close its till stops trading.
    """
    shift = Shift.objects.select_for_update().get(pk=shift.pk)

    if shift.state != ShiftState.OPEN:
        raise ShiftError("This drawer is already closed.", "shift_not_open")
    if declared_closing_cents < 0:
        raise ShiftError("A counted amount cannot be negative.", "negative_count")

    if denominations:
        counted = sum(
            denomination * quantity for denomination, quantity in denominations.items()
        )
        if counted != declared_closing_cents:
            # Refused, not silently corrected. The two figures disagreeing means
            # the cashier miscounted one of them, and picking a winner would
            # hide which.
            raise ShiftError(
                f"The notes counted come to {counted}, but the declared total is "
                f"{declared_closing_cents}. Check the count.",
                "count_does_not_add_up",
            )
        for denomination, quantity in denominations.items():
            DrawerCount.objects.create(
                tenant=shift.tenant,
                shift=shift,
                denomination_cents=denomination,
                quantity=quantity,
            )

    position = drawer_position(shift)
    expected = position.expected_cents
    variance = declared_closing_cents - expected

    shift.state = ShiftState.CLOSED
    shift.closed_at = timezone.now()
    shift.declared_closing_cents = declared_closing_cents
    shift.expected_closing_cents = expected
    shift.variance_cents = variance
    shift.closed_by = closed_by
    shift.closing_note = note
    shift.save()

    if variance != 0:
        ShiftDiscrepancy.objects.create(
            tenant=shift.tenant,
            shift=shift,
            kind=ShiftDiscrepancy.Kind.VARIANCE,
            detail=(
                f"The drawer was counted at {declared_closing_cents} cents "
                f"against an expected {expected}. That is "
                f"{'short' if variance < 0 else 'over'} by {abs(variance)}."
            ),
            context={
                "declared_cents": declared_closing_cents,
                "variance_cents": variance,
                **position.as_dict(),
            },
        )

    if closed_by != shift.cashier:
        # Recorded on its own, because a manager closing somebody else's drawer
        # is a normal thing that happens - the cashier went home - and also
        # exactly what it would look like if it were not.
        ShiftDiscrepancy.objects.create(
            tenant=shift.tenant,
            shift=shift,
            kind=ShiftDiscrepancy.Kind.FORCED_CLOSE,
            detail=(
                f"{closed_by.username} closed a drawer belonging to "
                f"{shift.cashier.username}."
            ),
            context={
                "closed_by": closed_by.username,
                "cashier": shift.cashier.username,
                "variance_cents": variance,
            },
        )

    record_audit(
        action=AuditAction.SHIFT_CLOSED,
        entity=shift,
        actor=closed_by,
        request=request,
        reason=note,
        after={
            "declared_cents": declared_closing_cents,
            "expected_cents": expected,
            "variance_cents": variance,
            "cashier": shift.cashier.username,
        },
    )
    return shift


@transaction.atomic
def record_cash_movement(
    *,
    shift: Shift,
    kind: str,
    amount_cents: int,
    reason: str,
    user,
    request=None,
) -> CashMovement:
    """Money crossing the drawer for something that is not a sale."""
    if shift.state != ShiftState.OPEN:
        raise ShiftError(
            "That drawer is closed. Cash cannot be moved through it.",
            "shift_not_open",
        )
    if amount_cents <= 0:
        raise ShiftError("An amount must be more than zero.", "invalid_amount")
    if not reason.strip():
        # 'Cash out' with no reason is indistinguishable from theft, and the
        # person who has to tell them apart is reading this months later.
        raise ShiftError("A cash movement needs a reason.", "reason_required")

    movement = CashMovement.objects.create(
        tenant=shift.tenant,
        shift=shift,
        kind=kind,
        amount_cents=amount_cents,
        reason=reason,
        user=user,
    )

    record_audit(
        action=AuditAction.CASH_MOVEMENT,
        entity=movement,
        actor=user,
        request=request,
        reason=reason,
        after={
            "kind": kind,
            "amount_cents": amount_cents,
            "shift": str(shift.id),
        },
    )
    return movement


def attribute_payment(*, payment: Payment, shift: Shift | None) -> None:
    """File a payment against the drawer it went into.

    If that drawer is already closed - an offline sale that synced days later -
    the shift's figures are **not** touched. They record what was true when
    somebody was accountable for them. The late arrival is recorded against the
    shift instead, with a foreign key back to it, so that joining a shift's
    frozen figures to whatever landed afterwards is a query rather than an
    investigation.
    """
    if shift is None:
        return

    if payment.method != PaymentMethod.CASH:
        # An M-Pesa payment never went into a drawer, so it has no bearing on
        # the count. Attributing it anyway would raise a late-attribution flag
        # every time a callback landed after a shift closed - noise about money
        # that was never in the till.
        return

    payment.shift = shift
    payment.save(update_fields=["shift", "updated_at"])

    if shift.state == ShiftState.OPEN:
        return

    ShiftDiscrepancy.objects.create(
        tenant=shift.tenant,
        shift=shift,
        kind=ShiftDiscrepancy.Kind.LATE_ATTRIBUTION,
        detail=(
            f"A {payment.get_method_display()} payment of {payment.amount_cents} "
            "cents arrived after this drawer was closed. The closing figures are "
            "left as they were counted."
        ),
        context={
            "payment_id": str(payment.id),
            "sale_id": str(payment.sale_id),
            "method": payment.method,
            "amount_cents": payment.amount_cents,
            "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
            "frozen_variance_cents": shift.variance_cents,
        },
    )


def resolve_shift(*, tenant, user, client_uuid=None) -> Shift | None:
    """Which drawer a sale belongs to.

    A till that was offline names the shift it recorded the sale under, by the
    identifier it generated itself. Anything else falls back to whatever drawer
    the acting user has open, which is the online case.

    Returns None when the business does not run shifts, and that is not an
    error - the whole feature is optional.
    """
    if client_uuid:
        # Scoped to the tenant, so another business's shift identifier is
        # simply not found rather than compared and rejected.
        return Shift.objects.filter(tenant=tenant, client_uuid=client_uuid).first()
    return open_shift_for(user)


def may_close(*, shift: Shift, user) -> bool:
    """Whether this person may close this drawer.

    Their own, always. Somebody else's only with manager rights - a cashier
    closing a colleague's drawer would be able to put a figure against
    another person's name.
    """
    if shift.cashier_id == user.id:
        return True
    return user.has_role_at_least(UserRole.MANAGER)
