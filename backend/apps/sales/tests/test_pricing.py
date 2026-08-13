"""
Cart arithmetic, and the places a cent could appear or vanish.

No database here - this is the pure arithmetic that every receipt depends on.
The properties asserted matter more than the individual cases: a total must
always equal the sum of its lines, an apportioned discount must always sum to
the discount, and net plus tax must always equal what the customer pays.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.sales.pricing import (
    LineInput,
    PricingError,
    apportion,
    change_for,
    price_cart,
    resolve_discount,
)

VAT = 1600


def line(
    price: int,
    quantity: str = "1",
    *,
    rate: int = VAT,
    inclusive: bool = True,
    discount_bps: int = 0,
    discount_cents: int = 0,
    name: str = "Item",
) -> LineInput:
    return LineInput(
        item_id="x",
        name=name,
        sku="SKU",
        unit="EACH",
        unit_price_cents=price,
        quantity=Decimal(quantity),
        tax_rate_bps=rate,
        tax_is_inclusive=inclusive,
        discount_bps=discount_bps,
        discount_cents=discount_cents,
    )


class TestLineExtension:
    def test_whole_units(self):
        assert line(18000, "3").gross_before_discount() == 54000

    def test_goods_sold_by_weight(self):
        """2.5 kg at KES 180 is exactly KES 450."""
        assert line(18000, "2.5").gross_before_discount() == 45000

    def test_an_awkward_weight_resolves_its_cent_once(self):
        # 0.333 kg at KES 180.00 is 5,994 cents exactly.
        assert line(18000, "0.333").gross_before_discount() == 5994

    def test_a_weight_that_lands_on_a_half_cent_rounds_up(self):
        # 1.005 x 100 cents = 100.5 cents.
        assert line(100, "1.005").gross_before_discount() == 101


class TestDiscountResolution:
    def test_a_percentage(self):
        assert resolve_discount(10000, discount_bps=1000, discount_cents=0) == 1000

    def test_a_fixed_amount(self):
        assert resolve_discount(10000, discount_bps=0, discount_cents=2500) == 2500

    def test_both_apply_together(self):
        """'10% off and another 50 shillings' is an ordinary promotion."""
        assert resolve_discount(10000, discount_bps=1000, discount_cents=5000) == 6000

    def test_a_discount_cannot_exceed_the_thing_discounted(self):
        """Otherwise the line goes negative and so, downstream, does the tax."""
        assert resolve_discount(10000, discount_bps=0, discount_cents=99999) == 10000

    def test_a_negative_discount_is_refused(self):
        with pytest.raises(PricingError):
            resolve_discount(10000, discount_bps=0, discount_cents=-1)

    def test_more_than_one_hundred_percent_is_refused(self):
        with pytest.raises(PricingError):
            resolve_discount(10000, discount_bps=10001, discount_cents=0)


class TestApportionment:
    """A cart discount split across lines must sum to exactly the discount."""

    def test_an_even_split(self):
        assert apportion(300, [100, 100, 100]) == [100, 100, 100]

    def test_a_split_that_does_not_divide_evenly(self):
        parts = apportion(100, [100, 100, 100])
        assert sum(parts) == 100
        assert sorted(parts) == [33, 33, 34]

    def test_weighting_follows_line_size(self):
        parts = apportion(100, [900, 100])
        assert sum(parts) == 100
        assert parts[0] > parts[1]

    @pytest.mark.parametrize("amount", range(1, 200))
    def test_the_parts_always_sum_to_the_whole(self, amount):
        """The property, walked from one cent upward.

        One cent lost here would leave a total that does not match the sum of
        its lines, and the difference would then be silently absorbed into tax.
        """
        for weights in ([100, 100, 100], [1, 2, 3], [999, 1], [50, 50], [7]):
            assert sum(apportion(amount, weights)) == amount

    def test_a_free_cart_takes_no_discount(self):
        """Splitting equally instead would create negative lines."""
        assert apportion(500, [0, 0]) == [0, 0]

    def test_nothing_to_split(self):
        assert apportion(0, [100, 100]) == [0, 0]

    def test_the_result_is_deterministic(self):
        """Ties break on weight then position, not on dictionary order."""
        assert apportion(100, [100, 100, 100]) == apportion(100, [100, 100, 100])


class TestInclusiveTax:
    def test_a_single_line(self):
        totals = price_cart([line(18000)])

        assert totals.total_cents == 18000  # the customer pays the marked price
        assert totals.subtotal_cents == 15517
        assert totals.tax_cents == 2483
        assert totals.subtotal_cents + totals.tax_cents == totals.total_cents

    def test_several_lines_sum_exactly(self):
        totals = price_cart([line(18000), line(6500), line(12345)])

        assert totals.total_cents == sum(priced.gross_cents for priced in totals.lines)
        assert totals.tax_cents == sum(priced.tax_cents for priced in totals.lines)
        assert totals.subtotal_cents + totals.tax_cents == totals.total_cents

    def test_rounding_happens_per_line_not_on_the_total(self):
        """Three awkward lines, each rounded, then summed.

        Rounding the total instead would let it disagree with the lines printed
        above it - which is exactly the thing a customer checks.
        """
        totals = price_cart([line(333), line(333), line(333)])

        assert totals.total_cents == 999
        assert totals.tax_cents == sum(priced.tax_cents for priced in totals.lines)


class TestExclusiveTax:
    def test_tax_is_added_on_top(self):
        totals = price_cart([line(15517, inclusive=False)])

        assert totals.subtotal_cents == 15517
        assert totals.tax_cents == 2483
        assert totals.total_cents == 18000

    def test_a_cart_may_mix_both(self):
        """The case a per-business tax setting could not express."""
        totals = price_cart(
            [line(18000, inclusive=True), line(15517, inclusive=False)]
        )

        assert totals.lines[0].gross_cents == 18000
        assert totals.lines[1].gross_cents == 18000
        assert totals.total_cents == 36000
        assert totals.subtotal_cents + totals.tax_cents == totals.total_cents


class TestZeroRated:
    def test_an_untaxed_line_is_all_net(self):
        totals = price_cart([line(18000, rate=0)])

        assert totals.tax_cents == 0
        assert totals.subtotal_cents == 18000
        assert totals.total_cents == 18000

    def test_zero_rated_alongside_standard_rated(self):
        """Unprocessed foods are zero-rated, and are much of a duka's shelf."""
        totals = price_cart([line(21000, rate=0), line(18000)])

        assert totals.tax_cents == 2483
        assert totals.total_cents == 39000


class TestLineDiscounts:
    def test_a_discount_reduces_the_taxable_amount(self):
        """Discount before tax. Taxing first would charge tax nobody paid."""
        plain = price_cart([line(10000)])
        discounted = price_cart([line(10000, discount_bps=1000)])

        assert discounted.total_cents == 9000
        assert discounted.tax_cents < plain.tax_cents

    def test_a_fully_discounted_line_is_free_and_untaxed(self):
        totals = price_cart([line(10000, discount_cents=10000)])

        assert totals.total_cents == 0
        assert totals.tax_cents == 0


class TestCartDiscounts:
    def test_a_cart_discount_is_shared_across_lines(self):
        totals = price_cart([line(10000), line(10000)], cart_discount_cents=1000)

        assert sum(priced.cart_discount_share_cents for priced in totals.lines) == 1000
        assert totals.total_cents == 19000

    def test_a_cart_percentage_discount(self):
        totals = price_cart([line(10000), line(10000)], cart_discount_bps=1000)

        assert totals.discount_cents == 2000
        assert totals.total_cents == 18000

    def test_the_share_is_taxed_on_each_line_at_its_own_rate(self):
        """Why apportionment has to happen before tax at all.

        There is no single rate at which a whole-cart discount could be taxed,
        because each line carries its own.
        """
        totals = price_cart(
            [line(10000, rate=1600), line(10000, rate=0)], cart_discount_cents=1000
        )

        taxed, zero_rated = totals.lines
        assert taxed.tax_cents > 0
        assert zero_rated.tax_cents == 0
        assert totals.subtotal_cents + totals.tax_cents == totals.total_cents

    def test_line_and_cart_discounts_stack(self):
        totals = price_cart([line(10000, discount_cents=1000)], cart_discount_cents=500)

        assert totals.discount_cents == 1500
        assert totals.total_cents == 8500

    @pytest.mark.parametrize("discount", [1, 7, 33, 99, 100, 333, 1000, 4999])
    def test_the_total_always_matches_the_lines(self, discount):
        totals = price_cart(
            [line(1234), line(5678), line(999), line(4321)],
            cart_discount_cents=discount,
        )

        assert totals.total_cents == sum(priced.gross_cents for priced in totals.lines)
        assert totals.discount_cents == discount + 0
        assert totals.subtotal_cents + totals.tax_cents == totals.total_cents

    def test_a_cart_discount_cannot_exceed_the_cart(self):
        totals = price_cart([line(10000)], cart_discount_cents=999999)
        assert totals.total_cents == 0


class TestRefusals:
    def test_an_empty_cart_is_refused(self):
        with pytest.raises(PricingError, match="at least one line"):
            price_cart([])


class TestCashTender:
    def test_change_owed(self):
        assert change_for(20000, 18000) == 2000

    def test_exact_money(self):
        assert change_for(18000, 18000) == 0

    def test_too_little_is_refused(self):
        with pytest.raises(PricingError):
            change_for(17000, 18000)
