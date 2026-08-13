"""
Money arithmetic, especially where it rounds.

Rounding is where money code goes wrong, and it goes wrong quietly: a receipt
that is one cent out looks fine to everyone except the customer who added it up
themselves, and a till that is a few shillings out every day looks like theft
rather than arithmetic. These tests pin the edges down.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.core.money import (
    apply_percentage,
    format_money,
    from_cents,
    round_cash,
    round_half_up_div,
    split_exclusive,
    split_inclusive,
    tax_from_inclusive,
    tax_on_exclusive,
    to_cents,
)


class TestRounding:
    """Halves round away from zero, the way a person does it by hand."""

    @pytest.mark.parametrize(
        ("numerator", "denominator", "expected"),
        [
            (10, 4, 3),   # 2.5 rounds up, not to the even 2
            (14, 4, 4),   # 3.5 rounds up
            (6, 4, 2),    # 1.5 rounds up
            (2, 4, 1),    # 0.5 rounds up
            (1, 3, 0),    # 0.333 rounds down
            (2, 3, 1),    # 0.667 rounds up
            (0, 7, 0),
            (-10, 4, -3),  # away from zero in both directions
            (-2, 4, -1),
        ],
    )
    def test_half_up_division(self, numerator, denominator, expected):
        assert round_half_up_div(numerator, denominator) == expected

    def test_banker_rounding_is_not_used(self):
        """Python's default would round 2.5 to 2 and 3.5 to 4. This must not."""
        assert round_half_up_div(5, 2) == 3
        assert round_half_up_div(7, 2) == 4

    def test_zero_denominator_is_refused(self):
        with pytest.raises(ValueError):
            round_half_up_div(1, 0)


class TestConversion:
    """Getting amounts in and out without losing precision."""

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            ("0.01", 1),
            ("0.10", 10),
            ("1.00", 100),
            ("120.50", 12050),
            ("0.005", 1),    # rounds up at the half cent
            ("0.004", 0),
            ("1999.99", 199999),
        ],
    )
    def test_to_cents(self, amount, expected):
        assert to_cents(Decimal(amount)) == expected

    def test_a_float_is_refused_rather_than_silently_rounded(self):
        """By the time a float arrives the precision is already gone.

        Accepting it here would hide the loss at the one point where it is
        still obvious what went wrong.
        """
        with pytest.raises(TypeError):
            to_cents(120.50)

    def test_whole_numbers_convert_without_a_decimal(self):
        assert to_cents(120) == 12000

    def test_round_trip_is_lossless(self):
        assert from_cents(to_cents(Decimal("847.35"))) == Decimal("847.35")

    def test_formatting_matches_a_receipt(self):
        assert format_money(12050) == "KES 120.50"
        assert format_money(100000) == "KES 1,000.00"
        assert format_money(0) == "KES 0.00"


class TestInclusiveTax:
    """Tax contained within a marked price, the normal Kenyan retail case."""

    def test_standard_vat_on_a_round_price(self):
        """KES 116.00 including 16% VAT is KES 100.00 plus KES 16.00."""
        net, tax = split_inclusive(11600, 1600)
        assert (net, tax) == (10000, 1600)

    def test_net_plus_tax_always_equals_the_price_charged(self):
        """The property that keeps receipts internally consistent.

        Deriving the net by subtraction rather than by a second rounded
        calculation is what guarantees this. Any price, any rate.
        """
        for gross in range(1, 2000):
            for rate in (0, 800, 1600, 2500):
                net, tax = split_inclusive(gross, rate)
                assert net + tax == gross

    @pytest.mark.parametrize(
        ("gross", "rate_bps", "expected_tax"),
        [
            (100, 1600, 14),     # 1.00 -> 0.14; the exact value is 0.1379
            (1, 1600, 0),        # a single cent carries no separable tax
            (7, 1600, 1),        # 0.0966 rounds up to 1
            (12345, 1600, 1703),
            (18000, 1600, 2483),  # a KES 180 bag of sugar
        ],
    )
    def test_known_awkward_amounts(self, gross, rate_bps, expected_tax):
        assert tax_from_inclusive(gross, rate_bps) == expected_tax

    def test_zero_rated_goods_carry_no_tax(self):
        """Unprocessed foods are zero-rated, and are a large part of a duka."""
        assert split_inclusive(18000, 0) == (18000, 0)

    def test_a_zero_amount_is_handled(self):
        assert split_inclusive(0, 1600) == (0, 0)

    def test_a_negative_rate_is_refused(self):
        with pytest.raises(ValueError):
            tax_from_inclusive(1000, -100)


class TestExclusiveTax:
    """Tax added on top, used for trade and wholesale pricing."""

    def test_standard_vat_added_on_top(self):
        gross, tax = split_exclusive(10000, 1600)
        assert (gross, tax) == (11600, 1600)

    def test_gross_always_equals_net_plus_tax(self):
        for net in range(1, 2000):
            for rate in (0, 800, 1600, 2500):
                gross, tax = split_exclusive(net, rate)
                assert gross == net + tax

    @pytest.mark.parametrize(
        ("net", "rate_bps", "expected_tax"),
        [
            (100, 1600, 16),
            (1, 1600, 0),      # 0.16 of a cent rounds down
            (4, 1600, 1),      # 0.64 of a cent rounds up
            (12345, 1600, 1975),
        ],
    )
    def test_known_awkward_amounts(self, net, rate_bps, expected_tax):
        assert tax_on_exclusive(net, rate_bps) == expected_tax

    def test_inclusive_and_exclusive_agree_on_a_round_case(self):
        """The two directions must reconcile, or one of them is wrong."""
        net_from_inclusive, tax_in = split_inclusive(11600, 1600)
        gross_from_exclusive, tax_ex = split_exclusive(10000, 1600)
        assert net_from_inclusive == 10000
        assert gross_from_exclusive == 11600
        assert tax_in == tax_ex


class TestDiscounts:
    """Percentage discounts, which round the same way tax does."""

    @pytest.mark.parametrize(
        ("amount", "percent_bps", "expected"),
        [
            (10000, 1000, 1000),   # 10% of 100.00
            (10000, 1250, 1250),   # 12.5%
            (333, 1000, 33),       # 33.3 rounds down
            (335, 1000, 34),       # 33.5 rounds up
            (10000, 0, 0),
            (10000, 10000, 10000),  # 100% off
        ],
    )
    def test_percentage_of_an_amount(self, amount, percent_bps, expected):
        assert apply_percentage(amount, percent_bps) == expected


class TestCashRounding:
    """Cash rounds to the shilling; there is no coin below KES 1 in use."""

    @pytest.mark.parametrize(
        ("cents", "expected"),
        [
            (18049, 18000),  # 180.49 -> 180
            (18050, 18100),  # 180.50 -> 181, half rounds up
            (18051, 18100),
            (18000, 18000),  # already whole
            (49, 0),
            (50, 100),
            (0, 0),
        ],
    )
    def test_rounds_to_the_nearest_shilling(self, cents, expected):
        assert round_cash(cents) == expected

    def test_the_rounding_difference_is_never_more_than_half_a_shilling(self):
        """Bounds the adjustment recorded against each cash sale."""
        for cents in range(0, 5000):
            assert abs(round_cash(cents) - cents) <= 50

    def test_a_non_positive_increment_is_refused(self):
        with pytest.raises(ValueError):
            round_cash(1000, increment_cents=0)
