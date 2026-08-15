"""
Which day a figure lands in, and what the figure is.

Most of these are about boundaries, because that is where reports go wrong in
ways nobody notices for a month.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from apps.core.tenancy import tenant_context
from apps.reports.periods import (
    DAY,
    MONTH,
    PeriodError,
    day_period,
    month_period,
    period_for,
    periods_between,
    tenant_zone,
    week_period,
)
from apps.reports.queries import (
    BY_QUANTITY,
    BY_REVENUE,
    best_sellers,
    cashier_figures,
    refund_reasons,
    sales_summary,
)
from apps.sales.models import Sale

NAIROBI = ZoneInfo("Africa/Nairobi")
CHECKOUT = "/api/v1/sales/checkout/cash/"


def sell(client, item, *, tendered=18000, quantity="1") -> dict:
    return client.post(
        CHECKOUT,
        {
            "lines": [{"item_id": str(item.id), "quantity": quantity}],
            "tendered_cents": tendered,
        },
        format="json",
    ).json()


def move_sale_to(sale_id, moment, tenant_id):
    """Backdate a sale's server timestamp, as a real one would have."""
    with tenant_context(tenant_id):
        Sale.objects.filter(pk=sale_id).update(server_received_at=moment)


@pytest.mark.django_db
class TestTheBusinessesOwnDay:
    def test_a_day_runs_local_midnight_to_local_midnight(self, tenant_a):
        period = day_period(date(2026, 8, 15), zone=NAIROBI)

        assert period.start.isoformat() == "2026-08-15T00:00:00+03:00"
        assert period.end.isoformat() == "2026-08-16T00:00:00+03:00"

    def test_a_day_is_not_utc(self, tenant_a):
        """A Nairobi shop's Tuesday is not UTC Tuesday. A report ending at 3am
        local is one nobody trusts twice."""
        period = day_period(date(2026, 8, 15), zone=NAIROBI)

        assert period.start.astimezone(ZoneInfo("UTC")).hour == 21
        assert period.start.astimezone(ZoneInfo("UTC")).day == 14

    def test_an_evening_sale_lands_on_the_local_day(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """The case the whole decision exists for: a shop trading at 10pm
        Nairobi is already tomorrow in UTC."""
        settled = sell(client_cashier_a, item_a)
        evening = datetime(2026, 8, 15, 22, 30, tzinfo=NAIROBI)
        move_sale_to(settled["id"], evening, tenant_a.id)

        period = period_for(tenant_a, granularity=DAY, on=date(2026, 8, 15))

        with tenant_context(tenant_a.id):
            summary = sales_summary(tenant_a, period)

        assert summary.sale_count == 1

    def test_a_sale_just_before_midnight_is_not_in_tomorrow(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a)
        move_sale_to(
            settled["id"], datetime(2026, 8, 15, 23, 59, 59, tzinfo=NAIROBI), tenant_a.id
        )

        tomorrow = period_for(tenant_a, granularity=DAY, on=date(2026, 8, 16))

        with tenant_context(tenant_a.id):
            assert sales_summary(tenant_a, tomorrow).sale_count == 0

    def test_a_sale_at_midnight_belongs_to_the_new_day(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """Half-open windows: consecutive periods neither overlap nor gap."""
        settled = sell(client_cashier_a, item_a)
        move_sale_to(
            settled["id"], datetime(2026, 8, 16, 0, 0, 0, tzinfo=NAIROBI), tenant_a.id
        )

        with tenant_context(tenant_a.id):
            yesterday = sales_summary(
                tenant_a, period_for(tenant_a, granularity=DAY, on=date(2026, 8, 15))
            )
            today = sales_summary(
                tenant_a, period_for(tenant_a, granularity=DAY, on=date(2026, 8, 16))
            )

        assert yesterday.sale_count == 0
        assert today.sale_count == 1

    def test_an_unknown_timezone_falls_back_rather_than_raising(self, tenant_a):
        """A drifted setting should shift a boundary slightly, which is
        visible, not break the page, which is not."""
        tenant_a.timezone = "Mars/Olympus_Mons"

        assert tenant_zone(tenant_a).key == "Africa/Nairobi"

    def test_a_business_in_another_zone_gets_its_own_boundary(self, tenant_a):
        tenant_a.timezone = "UTC"

        period = period_for(tenant_a, granularity=DAY, on=date(2026, 8, 15))

        assert period.start.isoformat() == "2026-08-15T00:00:00+00:00"


@pytest.mark.django_db
class TestBucketing:
    def test_a_week_runs_monday_to_monday(self):
        period = week_period(date(2026, 8, 15), zone=NAIROBI)  # a Saturday

        assert period.start.date() == date(2026, 8, 10)
        assert period.end.date() == date(2026, 8, 17)
        assert period.label == "2026-W33"

    def test_a_month_covers_the_calendar_month(self):
        period = month_period(date(2026, 8, 15), zone=NAIROBI)

        assert period.start.date() == date(2026, 8, 1)
        assert period.end.date() == date(2026, 9, 1)
        assert period.label == "2026-08"

    def test_december_rolls_into_january(self):
        period = month_period(date(2026, 12, 9), zone=NAIROBI)

        assert period.end.date() == date(2027, 1, 1)

    def test_a_range_yields_one_period_per_bucket(self, tenant_a):
        periods = periods_between(
            tenant_a, granularity=DAY, since=date(2026, 8, 1), until=date(2026, 8, 5)
        )

        assert [period.label for period in periods] == [
            "2026-08-01",
            "2026-08-02",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
        ]

    def test_a_month_range_does_not_repeat_a_bucket(self, tenant_a):
        periods = periods_between(
            tenant_a, granularity=MONTH, since=date(2026, 8, 1), until=date(2026, 8, 31)
        )

        assert len(periods) == 1

    def test_a_backwards_range_is_refused(self, tenant_a):
        with pytest.raises(PeriodError):
            periods_between(
                tenant_a, granularity=DAY, since=date(2026, 8, 5), until=date(2026, 8, 1)
            )

    def test_an_absurd_range_is_refused(self, tenant_a):
        """A mistyped range must not ask the database for ten thousand
        buckets."""
        with pytest.raises(PeriodError, match="coarser"):
            periods_between(
                tenant_a,
                granularity=DAY,
                since=date(2000, 1, 1),
                until=date(2026, 8, 15),
            )

    def test_an_unknown_granularity_is_refused(self, tenant_a):
        with pytest.raises(PeriodError):
            period_for(tenant_a, granularity="fortnight")


@pytest.mark.django_db
class TestWhatCountsAsRevenue:
    def test_a_paid_sale_counts(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.sale_count == 1
        assert summary.gross_cents == 18000

    def test_a_voided_sale_appears_nowhere_in_revenue(
        self, client_cashier_a, client_manager_a, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        """A void must not be buried inside a revenue query - a rising void
        count is a signal."""
        from apps.sales.services import LineRequest, create_sale, void_sale

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            void_sale(sale=sale, user=cashier_a, reason="Customer changed their mind")

            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.sale_count == 0
        assert summary.gross_cents == 0

    def test_a_voided_sale_is_counted_on_its_own(
        self, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        from apps.sales.services import LineRequest, create_sale, void_sale

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            void_sale(sale=sale, user=cashier_a, reason="Changed their mind")

            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.void_count == 1

    def test_an_open_sale_is_not_revenue(
        self, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        """Nobody has paid yet."""
        from apps.sales.services import LineRequest, create_sale

        with tenant_context(tenant_a.id):
            create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.sale_count == 0


@pytest.mark.django_db
class TestTheTenderSplit:
    def test_cash_is_reported_on_its_own(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """A shop reconciling a drawer needs the cash figure alone."""
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.taken.cash_cents == 18000
        assert summary.taken.mpesa_cents == 0
        assert summary.taken.total_cents == 18000

    def test_mpesa_is_reported_beside_it(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        from apps.sales.models import Payment, PaymentMethod

        settled = sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=settled["id"])
            Payment.objects.create(
                tenant=tenant_a,
                sale=sale,
                method=PaymentMethod.MPESA,
                amount_cents=5000,
                user=cashier_a,
            )
            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.taken.cash_cents == 18000
        assert summary.taken.mpesa_cents == 5000
        assert summary.taken.total_cents == 23000


@pytest.mark.django_db
class TestRefundsLandWhenTheyWereIssued:
    def _sale_and_refund(self, client, tenant, cashier, item, *, sold_on, refunded_on):
        from apps.sales.models import Refund
        from apps.sales.services import refund_sale

        settled = sell(client, item)
        move_sale_to(settled["id"], sold_on, tenant.id)

        with tenant_context(tenant.id):
            sale = Sale.objects.get(pk=settled["id"])
            refund = refund_sale(
                sale=sale, user=cashier, amount_cents=5000, reason="Damaged"
            )
            Refund.objects.filter(pk=refund.pk).update(created_at=refunded_on)
        return settled

    def test_a_refund_lands_in_the_month_it_was_issued(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """Revenue for a closed month must not change retroactively - the same
        principle as a frozen shift close."""
        july = datetime(2026, 7, 20, 12, 0, tzinfo=NAIROBI)
        august = datetime(2026, 8, 10, 12, 0, tzinfo=NAIROBI)
        self._sale_and_refund(
            client_cashier_a, tenant_a, cashier_a, item_a,
            sold_on=july, refunded_on=august,
        )

        with tenant_context(tenant_a.id):
            july_figures = sales_summary(
                tenant_a, period_for(tenant_a, granularity=MONTH, on=date(2026, 7, 1))
            )
            august_figures = sales_summary(
                tenant_a, period_for(tenant_a, granularity=MONTH, on=date(2026, 8, 1))
            )

        assert july_figures.gross_cents == 18000
        assert july_figures.refunded.total_cents == 0
        assert august_figures.refunded.total_cents == 5000

    def test_the_original_sale_is_still_reachable(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """Both are visible: the refund names its sale."""
        from apps.sales.models import Refund

        july = datetime(2026, 7, 20, 12, 0, tzinfo=NAIROBI)
        august = datetime(2026, 8, 10, 12, 0, tzinfo=NAIROBI)
        settled = self._sale_and_refund(
            client_cashier_a, tenant_a, cashier_a, item_a,
            sold_on=july, refunded_on=august,
        )

        with tenant_context(tenant_a.id):
            refund = Refund.objects.get()

        assert str(refund.sale_id) == settled["id"]

    def test_the_refund_rate_uses_the_periods_own_gross(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a)

        from apps.sales.services import refund_sale

        with tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=settled["id"])
            refund_sale(sale=sale, user=cashier_a, amount_cents=1800, reason="Damaged")
            summary = sales_summary(tenant_a, period_for(tenant_a))

        # 1800 of 18000 is ten percent.
        assert summary.refund_rate_bps == 1000

    def test_an_empty_period_has_no_rate_rather_than_a_crash(self, tenant_a):
        with tenant_context(tenant_a.id):
            summary = sales_summary(tenant_a, period_for(tenant_a))

        assert summary.refund_rate_bps == 0
        assert summary.average_basket_cents == 0


@pytest.mark.django_db
class TestBestSellers:
    @pytest.fixture
    def two_items(self, tenant_a, tax_rate_a, store_a):
        from apps.catalog.models import Item
        from apps.inventory.models import MovementReason, StockItem, apply_movement

        with tenant_context(tenant_a.id):
            cheap = Item.objects.create(
                tenant=tenant_a, sku="MATCH", name="Matchbox", price_cents=500
            )
            dear = Item.objects.create(
                tenant=tenant_a, sku="OIL", name="Cooking oil 5L", price_cents=120000
            )
            for item in (cheap, dear):
                stock = StockItem.objects.create(
                    tenant=tenant_a, item=item, store=store_a
                )
                apply_movement(
                    stock_item=stock,
                    delta=500,
                    reason=MovementReason.PURCHASE,
                    note="Opening",
                )
            return cheap, dear

    def test_ranking_by_quantity_and_by_revenue_differ(
        self, client_cashier_a, tenant_a, two_items
    ):
        """A crate of matchboxes outsells everything and earns almost nothing.
        Ranking by revenue alone hides what moves off the shelf."""
        cheap, dear = two_items
        sell(client_cashier_a, cheap, tendered=50000, quantity="100")
        sell(client_cashier_a, dear, tendered=120000, quantity="1")

        with tenant_context(tenant_a.id):
            period = period_for(tenant_a)
            by_revenue = best_sellers(tenant_a, period, order=BY_REVENUE)
            by_quantity = best_sellers(tenant_a, period, order=BY_QUANTITY)

        assert by_revenue[0].name == "Cooking oil 5L"
        assert by_quantity[0].name == "Matchbox"

    def test_quantity_is_a_string_not_a_float(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """A float would print 2.4999999999 on a report somebody is comparing
        against a shelf."""
        sell(client_cashier_a, item_a, tendered=45000, quantity="2.5")

        with tenant_context(tenant_a.id):
            sellers = best_sellers(tenant_a, period_for(tenant_a))

        assert sellers[0].as_dict()["quantity"] == "2.500"

    def test_it_reads_the_name_the_item_was_sold_under(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """An item renamed since still reports under the name it sold as - the
        same reason a reprinted receipt shows the old price."""
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            item_a.name = "Something else entirely"
            item_a.save()
            sellers = best_sellers(tenant_a, period_for(tenant_a))

        assert sellers[0].name == "Sugar 1kg"

    def test_a_void_does_not_appear(
        self, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        from apps.sales.services import LineRequest, create_sale, void_sale

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            void_sale(sale=sale, user=cashier_a, reason="Changed their mind")

            assert best_sellers(tenant_a, period_for(tenant_a)) == []


@pytest.mark.django_db
class TestCashierFigures:
    def test_somebody_who_sold_nothing_does_not_appear(
        self, client_cashier_a, tenant_a, manager_a, item_a, stock_a
    ):
        """An absence is not a zero, and a row of zeroes against a name reads
        as a judgement the data does not support."""
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            figures = cashier_figures(tenant_a, period_for(tenant_a))

        assert [figure.username for figure in figures] == ["mary"]

    def test_the_denominators_are_carried_alongside_the_rates(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """A rate on its own supports no conclusion."""
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            figure = cashier_figures(tenant_a, period_for(tenant_a))[0]

        body = figure.as_dict()
        assert body["sale_count"] == 1
        assert body["discounted_sale_count"] == 0
        assert "discount_rate_bps" in body

    def test_an_average_basket_is_computed_from_the_period(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)
        sell(client_cashier_a, item_a, tendered=36000, quantity="2")

        with tenant_context(tenant_a.id):
            figure = cashier_figures(tenant_a, period_for(tenant_a))[0]

        assert figure.sale_count == 2
        assert figure.average_basket_cents == 27000

    def test_rates_are_basis_points_not_floats(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            figure = cashier_figures(tenant_a, period_for(tenant_a))[0]

        assert isinstance(figure.discount_rate_bps, int)
        assert isinstance(figure.void_rate_bps, int)


@pytest.mark.django_db
class TestRefundReasons:
    def test_reasons_come_back_most_costly_first(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        from apps.sales.services import refund_sale

        first = sell(client_cashier_a, item_a)
        second = sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            refund_sale(
                sale=Sale.objects.get(pk=first["id"]),
                user=cashier_a,
                amount_cents=1000,
                reason="Wrong size",
            )
            refund_sale(
                sale=Sale.objects.get(pk=second["id"]),
                user=cashier_a,
                amount_cents=9000,
                reason="Spoiled stock",
            )
            reasons = refund_reasons(tenant_a, period_for(tenant_a))

        assert reasons[0].reason == "Spoiled stock"
        assert reasons[0].amount_cents == 9000


@pytest.mark.django_db
class TestNothingIsWritten:
    def test_running_every_report_changes_no_row(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """A report that wrote would eventually write something wrong."""
        from apps.sales.models import Payment

        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            before = (
                Sale.objects.count(),
                Payment.objects.count(),
                Sale.objects.first().updated_at,
            )
            period = period_for(tenant_a)
            sales_summary(tenant_a, period)
            best_sellers(tenant_a, period)
            cashier_figures(tenant_a, period)
            refund_reasons(tenant_a, period)
            after = (
                Sale.objects.count(),
                Payment.objects.count(),
                Sale.objects.first().updated_at,
            )

        assert before == after
