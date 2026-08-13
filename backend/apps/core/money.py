"""
Money arithmetic.

Every monetary amount in this system is an integer number of minor units
(cents). Not a float, and not a Decimal in the database. Floats cannot
represent 0.10 exactly, so a day of sales accumulates error that shows up as a
till that will not balance; Decimals are exact but invite accidental division
that produces unrepresentable amounts. Integers make the representable set the
same as the set of amounts a drawer can actually hold.

Rounding is half-up rather than Python's default banker's rounding. Half-up is
what a person does by hand, what Kenyan retail expects, and what makes a
receipt total match a customer's own arithmetic. Getting a fraction of a cent
"more correct" on average is worth nothing next to a customer disputing a
receipt they added up themselves.

Tax is expressed in basis points (16% is 1600) so that the rate itself is an
integer and the whole calculation stays in integer arithmetic.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Basis points in 100%. A rate of 1600 bps is 16%.
BPS_DENOMINATOR = 10_000

#: Kenyan cash rounds to the shilling: coins below KES 1 are no longer in use.
DEFAULT_CASH_ROUNDING_CENTS = 100


def round_half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounding halves away from zero.

    Implemented without floats or Decimals so that no amount can drift by a
    representation error before it is rounded.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator < 0:
        return -((2 * -numerator + denominator) // (2 * denominator))
    return (2 * numerator + denominator) // (2 * denominator)


def to_cents(amount: Decimal | int | str) -> int:
    """Convert a major-unit amount (as written on a shelf label) into cents.

    Accepts a string or Decimal. Passing a float is rejected rather than
    quietly accepted, because by the time a float arrives the precision loss
    has already happened and rounding it here would only hide that.
    """
    if isinstance(amount, float):
        raise TypeError(
            "Refusing to convert a float to cents; pass a Decimal or a string "
            "so the amount is exact before it is rounded."
        )
    if isinstance(amount, int):
        return amount * 100
    quantised = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantised * 100)


def from_cents(cents: int) -> Decimal:
    """Convert cents back into a major-unit Decimal for display or export."""
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def format_money(cents: int, currency: str = "KES") -> str:
    """Render an amount the way it appears on a receipt."""
    return f"{currency} {from_cents(cents):,.2f}"


def tax_from_inclusive(gross_cents: int, rate_bps: int) -> int:
    """Extract the tax contained within a tax-inclusive amount.

    Given a shelf price the customer actually pays, this is the portion that
    belongs to the tax authority::

        tax = gross * rate / (10000 + rate)

    Used when a tax rate is marked inclusive, which is the normal case for
    Kenyan retail: the customer expects to hand over exactly the marked price.
    """
    if rate_bps < 0:
        raise ValueError("rate_bps must not be negative")
    return round_half_up_div(gross_cents * rate_bps, BPS_DENOMINATOR + rate_bps)


def tax_on_exclusive(net_cents: int, rate_bps: int) -> int:
    """Calculate tax added on top of a tax-exclusive amount."""
    if rate_bps < 0:
        raise ValueError("rate_bps must not be negative")
    return round_half_up_div(net_cents * rate_bps, BPS_DENOMINATOR)


def split_inclusive(gross_cents: int, rate_bps: int) -> tuple[int, int]:
    """Split a tax-inclusive amount into (net, tax).

    The net is derived by subtraction rather than calculated independently, so
    that net + tax is always exactly the amount charged. Calculating both and
    rounding each separately is how receipts end up off by a cent.
    """
    tax = tax_from_inclusive(gross_cents, rate_bps)
    return gross_cents - tax, tax


def split_exclusive(net_cents: int, rate_bps: int) -> tuple[int, int]:
    """Split a tax-exclusive amount into (gross, tax)."""
    tax = tax_on_exclusive(net_cents, rate_bps)
    return net_cents + tax, tax


def apply_percentage(amount_cents: int, percent_bps: int) -> int:
    """Apply a percentage expressed in basis points, for discounts."""
    return round_half_up_div(amount_cents * percent_bps, BPS_DENOMINATOR)


def round_cash(
    amount_cents: int, increment_cents: int = DEFAULT_CASH_ROUNDING_CENTS
) -> int:
    """Round an amount to the smallest coin that actually circulates.

    Card and mobile money settle to the cent, but cash cannot: there is no coin
    below KES 1 in practical circulation. The difference between the rounded and
    unrounded totals is recorded on the sale so that a till reconciles exactly
    rather than drifting by a few shillings a day.
    """
    if increment_cents <= 0:
        raise ValueError("increment_cents must be positive")
    return round_half_up_div(amount_cents, increment_cents) * increment_cents
