"""
Reconciling a shift's frozen figures against what arrived afterwards.

A manager closing up compares "today's sales" against "the drawer" and finds
they disagree. Both numbers are right. A shift's closing figures are **frozen**
at the moment somebody counted and put their name to them, and a sale that
synced after that is filed against the shift without touching them - which is
deliberate, because a variance a cashier signed off must not change under them
days later.

The cost of that decision is exactly this confusion, and the answer is to
**show both**, clearly separated, rather than to reconcile them silently. A
recomputed total would give two different correct answers to "what was this
shift's variance" depending on when you asked, which is the thing the freeze
exists to prevent.

This is the query the ``LATE_ATTRIBUTION`` foreign key was put on
``ShiftDiscrepancy`` for. Joining a shift's frozen figures to what landed after
it closed was meant to be a lookup rather than an investigation, and here it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Sum, Value
from django.db.models.functions import Coalesce

from apps.reports.periods import Period
from apps.sales.models import Payment, PaymentMethod
from apps.shifts.models import Shift, ShiftDiscrepancy, ShiftState

_ZERO = Value(0)


@dataclass(frozen=True)
class LateArrival:
    """One payment that reached a shift after it was closed."""

    payment_id: str
    sale_id: str
    amount_cents: int
    method: str
    arrived_at: object

    def as_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "sale_id": self.sale_id,
            "amount_cents": self.amount_cents,
            "method": self.method,
            "arrived_at": self.arrived_at,
        }


@dataclass(frozen=True)
class DrawerReconciliation:
    """A shift as counted, and everything that landed on it afterwards.

    The two are never added together. ``counted`` is what a person signed for;
    ``late`` is what turned up later. A manager reading both can see why the
    day's sales figure is higher than the drawer was, and can see exactly which
    sales account for the difference.
    """

    shift_id: str
    cashier: str
    store_code: str
    opened_at: object
    closed_at: object
    state: str

    # ---- Frozen. Never recomputed. --------------------------------------
    opening_float_cents: int = 0
    declared_closing_cents: int | None = None
    expected_closing_cents: int | None = None
    variance_cents: int | None = None

    # ---- Arrived afterwards. Shown beside, never merged. -----------------
    late: list[LateArrival] = field(default_factory=list)

    @property
    def late_cash_cents(self) -> int:
        """What the drawer would have held had these arrived in time.

        Cash only, because only cash was ever in the drawer. Presented as an
        explanation of the gap, not as a correction to the variance.
        """
        return sum(
            arrival.amount_cents
            for arrival in self.late
            if arrival.method == PaymentMethod.CASH
        )

    @property
    def late_count(self) -> int:
        return len(self.late)

    @property
    def is_reconciled(self) -> bool:
        """Whether the frozen figures tell the whole story.

        True when nothing arrived late. False does not mean anything is wrong -
        it means a manager comparing two numbers needs to see both.
        """
        return not self.late

    @property
    def explained_variance_cents(self) -> int | None:
        """What the variance would read as, had nothing arrived late.

        **Not a correction.** ``variance_cents`` stays exactly as it was
        counted; this is here so a manager can see that a drawer which looks
        900 short is 900 short *because* three sales synced an hour after
        close - and can then stop looking for missing cash. Null when the shift
        is still open, because there is no variance yet.
        """
        if self.variance_cents is None:
            return None
        return self.variance_cents + self.late_cash_cents

    def as_dict(self) -> dict:
        return {
            "shift_id": self.shift_id,
            "cashier": self.cashier,
            "store_code": self.store_code,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "state": self.state,
            "counted": {
                "opening_float_cents": self.opening_float_cents,
                "declared_closing_cents": self.declared_closing_cents,
                "expected_closing_cents": self.expected_closing_cents,
                "variance_cents": self.variance_cents,
            },
            "arrived_after_close": {
                "count": self.late_count,
                "cash_cents": self.late_cash_cents,
                "payments": [arrival.as_dict() for arrival in self.late],
            },
            "is_reconciled": self.is_reconciled,
            "explained_variance_cents": self.explained_variance_cents,
        }


def reconcile_shift(shift: Shift) -> DrawerReconciliation:
    """One shift's frozen figures, beside whatever arrived after it closed.

    The late arrivals are found through ``ShiftDiscrepancy`` rather than by
    comparing timestamps, because the discrepancy is the *record* that something
    arrived late - written at the moment it happened, by the code that knew. A
    timestamp comparison would re-derive it, and would quietly disagree the
    first time a clock or a definition moved.
    """
    late: list[LateArrival] = []

    if shift.state == ShiftState.CLOSED:
        flags = ShiftDiscrepancy.objects.filter(
            shift=shift, kind=ShiftDiscrepancy.Kind.LATE_ATTRIBUTION
        ).order_by("created_at")

        payment_ids = [
            flag.context.get("payment_id") for flag in flags if flag.context.get("payment_id")
        ]
        payments = {
            str(payment.id): payment
            for payment in Payment.objects.filter(id__in=payment_ids)
        }

        for flag in flags:
            payment = payments.get(flag.context.get("payment_id"))
            late.append(
                LateArrival(
                    payment_id=flag.context.get("payment_id", ""),
                    sale_id=flag.context.get("sale_id", ""),
                    amount_cents=flag.context.get("amount_cents", 0),
                    method=flag.context.get("method", ""),
                    # The payment's own timestamp when it is still there, the
                    # flag's when it is not. A payment can only vanish if a sale
                    # was removed, which the schema prevents - but reading
                    # through a missing row would turn a report into a 500.
                    arrived_at=payment.created_at if payment else flag.created_at,
                )
            )

    return DrawerReconciliation(
        shift_id=str(shift.id),
        cashier=shift.cashier.username if shift.cashier_id else "",
        store_code=shift.store.code if shift.store_id else "",
        opened_at=shift.opened_at,
        closed_at=shift.closed_at,
        state=shift.state,
        opening_float_cents=shift.opening_float_cents,
        declared_closing_cents=shift.declared_closing_cents,
        expected_closing_cents=shift.expected_closing_cents,
        variance_cents=shift.variance_cents,
        late=late,
    )


def shift_summary(tenant, period: Period) -> list[DrawerReconciliation]:
    """Every drawer opened in a period, with its reconciliation.

    Bucketed by ``opened_at``, not by close: a shift that ran past midnight
    belongs to the day it started, which is how the person who worked it thinks
    about it.
    """
    shifts = (
        Shift.objects.filter(
            tenant=tenant, opened_at__gte=period.start, opened_at__lt=period.end
        )
        .select_related("cashier", "store")
        .order_by("opened_at")
    )
    return [reconcile_shift(shift) for shift in shifts]


@dataclass(frozen=True)
class DrawerTotals:
    """The period's drawers, added up.

    Only the counted figures are summed. The late arrivals are counted and
    totalled separately, so the two never merge into one number that would be
    neither what was signed for nor what the sales say.
    """

    shift_count: int = 0
    open_shift_count: int = 0
    opening_float_cents: int = 0
    declared_cents: int = 0
    expected_cents: int = 0
    variance_cents: int = 0
    late_count: int = 0
    late_cash_cents: int = 0

    def as_dict(self) -> dict:
        return {
            "shift_count": self.shift_count,
            "open_shift_count": self.open_shift_count,
            "counted": {
                "opening_float_cents": self.opening_float_cents,
                "declared_cents": self.declared_cents,
                "expected_cents": self.expected_cents,
                "variance_cents": self.variance_cents,
            },
            "arrived_after_close": {
                "count": self.late_count,
                "cash_cents": self.late_cash_cents,
            },
        }


def drawer_totals(reconciliations: list[DrawerReconciliation]) -> DrawerTotals:
    """Add up a period's drawers without merging the two halves."""
    closed = [row for row in reconciliations if row.variance_cents is not None]
    return DrawerTotals(
        shift_count=len(reconciliations),
        open_shift_count=sum(1 for row in reconciliations if row.state == ShiftState.OPEN),
        opening_float_cents=sum(row.opening_float_cents for row in reconciliations),
        declared_cents=sum(row.declared_closing_cents or 0 for row in closed),
        expected_cents=sum(row.expected_closing_cents or 0 for row in closed),
        variance_cents=sum(row.variance_cents or 0 for row in closed),
        late_count=sum(row.late_count for row in reconciliations),
        late_cash_cents=sum(row.late_cash_cents for row in reconciliations),
    )


def cash_taken_in(tenant, period: Period) -> int:
    """Cash payments recorded in a period, whatever drawer they landed in.

    The figure a shift summary is compared *against*. Kept here beside the
    reconciliation so the two are read together: this counts every cash payment
    in the window, including ones filed against a drawer that had already
    closed, which is precisely why it can exceed what the drawers were counted
    at.
    """
    return (
        Payment.objects.filter(
            tenant=tenant,
            method=PaymentMethod.CASH,
            created_at__gte=period.start,
            created_at__lt=period.end,
        ).aggregate(total=Coalesce(Sum("amount_cents"), _ZERO))["total"]
    )


def unreconciled_shift_count(tenant) -> int:
    """Closed drawers with something filed against them afterwards.

    For a back-office screen: a number a manager can act on, without loading
    every shift the business has ever run.
    """
    return (
        ShiftDiscrepancy.objects.filter(
            tenant=tenant, kind=ShiftDiscrepancy.Kind.LATE_ATTRIBUTION
        )
        .values("shift_id")
        .aggregate(count=Count("shift_id", distinct=True))["count"]
    )
