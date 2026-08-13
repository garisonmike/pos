"""
What a sale costs.

Pure arithmetic, no database. Everything here is integer cents, and the rules
are the ones already proved in milestone 2: half-up rounding, tax rates in basis
points, and inclusive tax derived by subtraction so net plus tax is always
exactly what the customer pays.

Three rules govern the order of operations, and each exists because getting it
the other way round produces a receipt that is wrong in a way a customer can
see:

**Discount before tax.** A discount reduces the taxable amount. Taxing first and
discounting after would charge tax on money nobody paid.

**A cart discount is apportioned across lines before tax is computed.** Tax is
per line, because each line carries its own rate and its own inclusive flag, so
there is no single rate at which a whole-cart discount could be taxed. Leaving
it unapportioned would make the tax total unreconcilable with the lines.

**Round per line, then sum.** Never compute a total and round that. Rounding the
sum lets a total disagree with the lines printed above it, which is precisely
the thing a customer checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from apps.core.money import apply_percentage, split_exclusive, split_inclusive


class PricingError(ValueError):
    """A cart that cannot be priced, as opposed to one that prices to zero."""


@dataclass(frozen=True)
class LineInput:
    """One line as the till submits it."""

    item_id: str
    name: str
    sku: str
    unit: str
    unit_price_cents: int
    quantity: Decimal
    tax_rate_bps: int = 0
    tax_is_inclusive: bool = True
    #: Line discount, given either way. Both may be present; both are applied.
    discount_bps: int = 0
    discount_cents: int = 0

    def gross_before_discount(self) -> int:
        """Quantity times price, rounded once.

        Rounded here because quantity is fractional for goods sold by weight -
        2.5 kg at KES 180 is 450, but 0.333 kg is 59.94, and the cent has to be
        resolved somewhere. Doing it once, at the line, keeps every later figure
        exact.
        """
        exact = Decimal(self.unit_price_cents) * self.quantity
        return int(exact.to_integral_value(rounding="ROUND_HALF_UP"))


@dataclass
class LineTotals:
    """One priced line."""

    line: LineInput
    gross_before_discount_cents: int
    line_discount_cents: int
    cart_discount_share_cents: int
    net_cents: int
    tax_cents: int
    gross_cents: int

    @property
    def total_discount_cents(self) -> int:
        return self.line_discount_cents + self.cart_discount_share_cents


@dataclass
class CartTotals:
    """A whole priced cart."""

    lines: list[LineTotals] = field(default_factory=list)
    subtotal_cents: int = 0
    discount_cents: int = 0
    tax_cents: int = 0
    total_cents: int = 0

    @property
    def line_count(self) -> int:
        return len(self.lines)


def resolve_discount(base_cents: int, *, discount_bps: int, discount_cents: int) -> int:
    """Work out a discount in cents, however it was expressed.

    Percentage and fixed amount may both be present, and both apply - a "10% off
    and another 50 shillings" promotion is an ordinary thing to want. The result
    is capped at the base, because a discount larger than the thing being
    discounted would produce a negative line and, downstream, negative tax.
    """
    if discount_bps < 0 or discount_cents < 0:
        raise PricingError("A discount cannot be negative.")
    if discount_bps > 10_000:
        raise PricingError("A discount cannot exceed 100%.")

    total = apply_percentage(base_cents, discount_bps) + discount_cents
    return min(total, max(base_cents, 0))


def apportion(amount_cents: int, weights: list[int]) -> list[int]:
    """Split an amount across weights so the parts sum exactly to the whole.

    Largest-remainder: each weight takes its exact share floored, and the cents
    left over go to whichever lines were rounded down hardest, ties broken by
    the larger weight so the result is deterministic rather than dependent on
    dictionary order.

    The exactness is the point. A cart discount that apportions to one cent less
    than it should leaves a total that does not match the sum of its lines, and
    that discrepancy would then be silently absorbed into tax.
    """
    if amount_cents == 0 or not weights:
        return [0] * len(weights)
    if any(weight < 0 for weight in weights):
        raise PricingError("Weights cannot be negative.")

    total_weight = sum(weights)
    if total_weight == 0:
        # Every line is free, so there is nothing to take a discount off. The
        # alternative - splitting equally - would create negative lines.
        return [0] * len(weights)

    shares = [amount_cents * weight // total_weight for weight in weights]
    remainder = amount_cents - sum(shares)

    if remainder:
        # Rank by the fractional part that was discarded, largest first.
        fractions = [
            (amount_cents * weight) % total_weight
            for weight in weights
        ]
        order = sorted(
            range(len(weights)),
            key=lambda index: (-fractions[index], -weights[index], index),
        )
        for position in range(remainder):
            shares[order[position % len(order)]] += 1

    return shares


def price_cart(
    lines: list[LineInput],
    *,
    cart_discount_bps: int = 0,
    cart_discount_cents: int = 0,
) -> CartTotals:
    """Price a whole cart, lines and all.

    The sequence is: line gross, line discount, apportion the cart discount over
    what remains, then split tax on each line using that line's own rate and
    inclusive flag. Mixed inclusive and exclusive lines therefore total
    correctly on one sale, which is the case a per-business tax setting could
    not express.
    """
    if not lines:
        raise PricingError("A sale needs at least one line.")

    grosses = [line.gross_before_discount() for line in lines]
    line_discounts = [
        resolve_discount(
            gross, discount_bps=line.discount_bps, discount_cents=line.discount_cents
        )
        for line, gross in zip(lines, grosses, strict=True)
    ]
    after_line_discount = [
        gross - discount for gross, discount in zip(grosses, line_discounts, strict=True)
    ]

    cart_discount = resolve_discount(
        sum(after_line_discount),
        discount_bps=cart_discount_bps,
        discount_cents=cart_discount_cents,
    )
    shares = apportion(cart_discount, after_line_discount)

    totals = CartTotals()
    for line, gross, line_discount, share, base in zip(
        lines, grosses, line_discounts, shares, after_line_discount, strict=True
    ):
        charged = base - share

        if line.tax_rate_bps <= 0:
            net, tax = charged, 0
            line_gross = charged
        elif line.tax_is_inclusive:
            # The amount charged already contains the tax.
            net, tax = split_inclusive(charged, line.tax_rate_bps)
            line_gross = charged
        else:
            # Tax is added on top of the amount charged.
            line_gross, tax = split_exclusive(charged, line.tax_rate_bps)
            net = charged

        totals.lines.append(
            LineTotals(
                line=line,
                gross_before_discount_cents=gross,
                line_discount_cents=line_discount,
                cart_discount_share_cents=share,
                net_cents=net,
                tax_cents=tax,
                gross_cents=line_gross,
            )
        )

        totals.subtotal_cents += net
        totals.discount_cents += line_discount + share
        totals.tax_cents += tax
        totals.total_cents += line_gross

    return totals


def change_for(tendered_cents: int, due_cents: int) -> int:
    """Change owed on a cash tender."""
    if tendered_cents < due_cents:
        raise PricingError("Tendered less than the amount due.")
    return tendered_cents - due_cents

