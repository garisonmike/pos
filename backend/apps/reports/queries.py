"""
Where every reported figure comes from.

**Nothing here writes.** Every number is derived from the ledgers that already
exist - sales, payments, refunds, shifts - because a report that disagrees with
the sale it came from is worse than no report. There is no warehouse and no
star schema: this reads the operational tables, indexed where the query needs
it. A duka does a few hundred sales a day, and a denormalised copy would add a
synchronisation problem, and a second place for figures to be wrong, to solve a
performance problem nobody has.

Pure functions taking ``(tenant, period)`` and returning dataclasses. No
serialization, no HTTP. The API, the CSV, the PDF and the platform summary all
call these, so the four cannot disagree - the same arrangement that keeps the
receipt's text and PDF renderings honest.

Four rules govern every figure:

**The server's clock.** Buckets read ``server_received_at``, never the till's.

**The business's own day.** Boundaries come from ``Tenant.timezone``.

**Refunds land in the period they were issued in**, not the period of the sale
they correct. Revenue for a closed month must not change retroactively - the
same principle as a frozen shift close. The sale is still reachable, so both
are visible.

**Cash and total are reported separately.** A shop reconciling a drawer needs
the cash figure alone; a shop looking at takings needs everything. One combined
number serves neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from apps.reports.periods import Period
from apps.sales.models import (
    Payment,
    PaymentMethod,
    Refund,
    RefundMethod,
    Sale,
    SaleLine,
    SaleState,
)

#: States whose money counts as revenue.
#:
#: A void appears nowhere in revenue. It is counted on its own instead - a
#: rising void count is a signal, and burying it inside a revenue query is how
#: it stays unnoticed.
REVENUE_STATES = (
    SaleState.PAID,
    SaleState.PARTIALLY_REFUNDED,
    SaleState.REFUNDED,
)

_ZERO = Value(0)


def _cents(expression):
    """Sum to an integer, treating an empty set as zero rather than null."""
    return Coalesce(Sum(expression), _ZERO)


def sales_in(tenant, period: Period):
    """Every sale that counts as revenue in a period.

    Filtered on ``server_received_at`` because that is when the business
    received the money, whatever the till believed the time was.
    """
    return Sale.objects.filter(
        tenant=tenant,
        state__in=REVENUE_STATES,
        server_received_at__gte=period.start,
        server_received_at__lt=period.end,
    )


@dataclass(frozen=True)
class TenderSplit:
    """Money taken, by how it arrived.

    Kept apart rather than summed to one figure because the two reconcile
    against different things: cash against a drawer somebody counts, M-Pesa
    against a statement somebody downloads.
    """

    cash_cents: int = 0
    mpesa_cents: int = 0

    @property
    def total_cents(self) -> int:
        return self.cash_cents + self.mpesa_cents

    def as_dict(self) -> dict:
        return {
            "cash_cents": self.cash_cents,
            "mpesa_cents": self.mpesa_cents,
            "total_cents": self.total_cents,
        }


@dataclass(frozen=True)
class SalesSummary:
    """What a business took in one period."""

    period: Period
    sale_count: int = 0
    gross_cents: int = 0
    net_cents: int = 0
    tax_cents: int = 0
    discount_cents: int = 0
    rounding_cents: int = 0
    #: Written off because an offline till undercharged. Not a discount: nobody
    #: authorised it, and lumping the two together would hide a stale price
    #: list behind what looks like generosity.
    shortfall_cents: int = 0

    taken: TenderSplit = field(default_factory=TenderSplit)
    refunded: TenderSplit = field(default_factory=TenderSplit)
    refund_count: int = 0

    void_count: int = 0
    offline_sale_count: int = 0

    @property
    def net_taken_cents(self) -> int:
        """Money in, less money given back, in the period each happened."""
        return self.taken.total_cents - self.refunded.total_cents

    @property
    def average_basket_cents(self) -> int:
        if not self.sale_count:
            return 0
        return round(self.gross_cents / self.sale_count)

    @property
    def refund_rate_bps(self) -> int:
        """Refunds as a share of gross, in basis points.

        Basis points rather than a percentage, for the same reason tax rates
        are: a float here would eventually print a rate that does not match the
        figures above it.
        """
        if not self.gross_cents:
            return 0
        return round(self.refunded.total_cents * 10_000 / self.gross_cents)

    def as_dict(self) -> dict:
        return {
            **self.period.as_dict(),
            "sale_count": self.sale_count,
            "gross_cents": self.gross_cents,
            "net_cents": self.net_cents,
            "tax_cents": self.tax_cents,
            "discount_cents": self.discount_cents,
            "rounding_cents": self.rounding_cents,
            "shortfall_cents": self.shortfall_cents,
            "taken": self.taken.as_dict(),
            "refunded": self.refunded.as_dict(),
            "refund_count": self.refund_count,
            "net_taken_cents": self.net_taken_cents,
            "average_basket_cents": self.average_basket_cents,
            "refund_rate_bps": self.refund_rate_bps,
            "void_count": self.void_count,
            "offline_sale_count": self.offline_sale_count,
        }


def sales_summary(tenant, period: Period) -> SalesSummary:
    """What a business took in one period.

    Payments and refunds are summed from their **own** timestamps rather than
    from their sale's, so a refund issued in August against a July sale lands in
    August. July's figures were true when July closed and must stay that way.
    """
    sales = sales_in(tenant, period)

    totals = sales.aggregate(
        sale_count=Count("id"),
        gross=_cents("total_cents"),
        net=_cents("subtotal_cents"),
        tax=_cents("tax_cents"),
        discount=_cents("discount_cents"),
        rounding=_cents("rounding_adjustment_cents"),
        shortfall=_cents("offline_shortfall_cents"),
        offline=Count("id", filter=Q(was_offline=True)),
    )

    payments = Payment.objects.filter(
        tenant=tenant, created_at__gte=period.start, created_at__lt=period.end
    ).aggregate(
        cash=Coalesce(
            Sum("amount_cents", filter=Q(method=PaymentMethod.CASH)), _ZERO
        ),
        mpesa=Coalesce(
            Sum("amount_cents", filter=Q(method=PaymentMethod.MPESA)), _ZERO
        ),
    )

    refunds = Refund.objects.filter(
        tenant=tenant, created_at__gte=period.start, created_at__lt=period.end
    ).aggregate(
        count=Count("id"),
        cash=Coalesce(Sum("amount_cents", filter=Q(method=RefundMethod.CASH)), _ZERO),
        # An M-Pesa refund may be settled by hand, but it is still money the
        # business gave back and belongs in the figure.
        mpesa=Coalesce(
            Sum("amount_cents", filter=~Q(method=RefundMethod.CASH)), _ZERO
        ),
    )

    voids = Sale.objects.filter(
        tenant=tenant,
        state=SaleState.VOID,
        server_received_at__gte=period.start,
        server_received_at__lt=period.end,
    ).count()

    return SalesSummary(
        period=period,
        sale_count=totals["sale_count"],
        gross_cents=totals["gross"],
        net_cents=totals["net"],
        tax_cents=totals["tax"],
        discount_cents=totals["discount"],
        rounding_cents=totals["rounding"],
        shortfall_cents=totals["shortfall"],
        taken=TenderSplit(cash_cents=payments["cash"], mpesa_cents=payments["mpesa"]),
        refunded=TenderSplit(cash_cents=refunds["cash"], mpesa_cents=refunds["mpesa"]),
        refund_count=refunds["count"],
        void_count=voids,
        offline_sale_count=totals["offline"],
    )


def sales_series(tenant, periods: list[Period]) -> list[SalesSummary]:
    """One summary per period, oldest first."""
    return [sales_summary(tenant, period) for period in periods]


@dataclass(frozen=True)
class BestSeller:
    item_id: str
    name: str
    sku: str
    quantity: Decimal
    revenue_cents: int
    line_count: int

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "sku": self.sku,
            # A string, not a float. The quantity is a decimal with three
            # places and a float would print 2.4999999999 on a report somebody
            # is comparing against a shelf.
            "quantity": str(self.quantity),
            "revenue_cents": self.revenue_cents,
            "line_count": self.line_count,
        }


#: How a best-seller list may be ordered.
#:
#: Both, because they rank differently and a shop needs both readings. A crate
#: of matchboxes outsells everything by quantity and earns almost nothing;
#: ranking by revenue alone hides what actually moves off the shelf.
BY_QUANTITY = "quantity"
BY_REVENUE = "revenue"


def best_sellers(
    tenant, period: Period, *, order: str = BY_REVENUE, limit: int = 20
) -> list[BestSeller]:
    """What sold, ranked either way.

    Reads the sale's own snapshotted line names rather than joining to the
    catalogue, so an item renamed since still reports under the name it was
    sold as - the same reason a reprinted receipt shows the old price.
    """
    if order not in (BY_QUANTITY, BY_REVENUE):
        raise ValueError("Order by quantity or revenue.")

    rows = (
        SaleLine.objects.filter(
            tenant=tenant,
            sale__state__in=REVENUE_STATES,
            sale__server_received_at__gte=period.start,
            sale__server_received_at__lt=period.end,
        )
        .values("item_id", "name", "sku")
        .annotate(
            quantity=Coalesce(
                Sum("quantity"),
                Value(Decimal("0")),
                output_field=DecimalField(max_digits=14, decimal_places=3),
            ),
            revenue_cents=_cents("gross_cents"),
            line_count=Count("id"),
        )
        .order_by("-quantity" if order == BY_QUANTITY else "-revenue_cents", "name")
    )

    return [
        BestSeller(
            item_id=str(row["item_id"]) if row["item_id"] else "",
            name=row["name"],
            sku=row["sku"] or "",
            quantity=row["quantity"],
            revenue_cents=row["revenue_cents"],
            line_count=row["line_count"],
        )
        for row in rows[:limit]
    ]


@dataclass(frozen=True)
class CashierFigures:
    """One person's period.

    **Read the denominators, not the headline.** These figures do not say who is
    good at the job. A cashier on the quiet shift will always look worse per
    sale; a discount rate says nothing without knowing who authorised each one,
    and the authoriser is on the sale, not on the cashier. This exists to find
    a pattern worth asking about, not to rank people - see ARCHITECTURE.md.
    """

    user_id: str
    username: str
    full_name: str

    sale_count: int = 0
    gross_cents: int = 0
    discount_cents: int = 0
    discounted_sale_count: int = 0
    void_count: int = 0
    refund_count: int = 0
    refunded_cents: int = 0

    @property
    def average_basket_cents(self) -> int:
        if not self.sale_count:
            return 0
        return round(self.gross_cents / self.sale_count)

    @property
    def discount_rate_bps(self) -> int:
        """Discount as a share of gross, in basis points."""
        if not self.gross_cents:
            return 0
        return round(self.discount_cents * 10_000 / self.gross_cents)

    @property
    def void_rate_bps(self) -> int:
        total = self.sale_count + self.void_count
        if not total:
            return 0
        return round(self.void_count * 10_000 / total)

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "full_name": self.full_name,
            "sale_count": self.sale_count,
            "gross_cents": self.gross_cents,
            "discount_cents": self.discount_cents,
            "discounted_sale_count": self.discounted_sale_count,
            "void_count": self.void_count,
            "refund_count": self.refund_count,
            "refunded_cents": self.refunded_cents,
            "average_basket_cents": self.average_basket_cents,
            "discount_rate_bps": self.discount_rate_bps,
            "void_rate_bps": self.void_rate_bps,
        }


def cashier_figures(tenant, period: Period) -> list[CashierFigures]:
    """Every cashier who rang something up in a period.

    Somebody who worked and sold nothing does not appear, which is deliberate:
    an absence is not a zero, and a row of zeroes against a name reads as a
    judgement the data does not support.
    """
    rows = (
        sales_in(tenant, period)
        .values("cashier_id", "cashier__username", "cashier__full_name")
        .annotate(
            sale_count=Count("id"),
            gross=_cents("total_cents"),
            discount=_cents("discount_cents"),
            discounted=Count("id", filter=Q(discount_cents__gt=0)),
        )
        .order_by("-gross")
    )

    voids = dict(
        Sale.objects.filter(
            tenant=tenant,
            state=SaleState.VOID,
            server_received_at__gte=period.start,
            server_received_at__lt=period.end,
        )
        .values_list("cashier_id")
        .annotate(count=Count("id"))
    )

    refunds = {
        row["user_id"]: row
        for row in Refund.objects.filter(
            tenant=tenant, created_at__gte=period.start, created_at__lt=period.end
        )
        .values("user_id")
        .annotate(count=Count("id"), amount=_cents("amount_cents"))
    }

    figures = []
    for row in rows:
        user_id = row["cashier_id"]
        refund = refunds.get(user_id, {})
        figures.append(
            CashierFigures(
                user_id=str(user_id),
                username=row["cashier__username"],
                full_name=row["cashier__full_name"],
                sale_count=row["sale_count"],
                gross_cents=row["gross"],
                discount_cents=row["discount"],
                discounted_sale_count=row["discounted"],
                void_count=voids.get(user_id, 0),
                refund_count=refund.get("count", 0),
                refunded_cents=refund.get("amount", 0),
            )
        )
    return figures


@dataclass(frozen=True)
class RefundReason:
    reason: str
    count: int
    amount_cents: int

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "count": self.count,
            "amount_cents": self.amount_cents,
        }


def refund_reasons(tenant, period: Period, *, limit: int = 20) -> list[RefundReason]:
    """Why money went back, most costly first.

    A refund rate on its own says nothing worth acting on. The reasons are
    where a shop finds that one supplier's stock keeps coming back.
    """
    rows = (
        Refund.objects.filter(
            tenant=tenant, created_at__gte=period.start, created_at__lt=period.end
        )
        .values("reason")
        .annotate(count=Count("id"), amount=_cents("amount_cents"))
        .order_by("-amount")
    )
    return [
        RefundReason(
            reason=row["reason"] or "(no reason given)",
            count=row["count"],
            amount_cents=row["amount"],
        )
        for row in rows[:limit]
    ]
