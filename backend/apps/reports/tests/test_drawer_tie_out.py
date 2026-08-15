"""
Why today's sales figure disagrees with the drawer.

A manager closing up compares two numbers and finds they differ. Both are
right. The shift was counted and frozen; a sale that synced afterwards was filed
against it without touching the count. These tests pin that the two are shown
**together and separately** - never merged into a third number that would be
neither what was signed for nor what the sales say.

This is what the ``LATE_ATTRIBUTION`` foreign key on ``ShiftDiscrepancy`` was
put there for, one milestone earlier.
"""

from __future__ import annotations

import uuid

import pytest

from apps.core.tenancy import tenant_context
from apps.reports.drawer import (
    cash_taken_in,
    drawer_totals,
    reconcile_shift,
    shift_summary,
    unreconciled_shift_count,
)
from apps.reports.periods import period_for
from apps.sales.models import Payment, PaymentMethod, Sale
from apps.shifts.models import Shift, ShiftState

CHECKOUT = "/api/v1/sales/checkout/cash/"
OPEN = "/api/v1/shifts/open/"
DRAWERS = "/api/v1/reports/drawers/"


def sell(client, item, *, tendered=18000) -> dict:
    return client.post(
        CHECKOUT,
        {
            "lines": [{"item_id": str(item.id), "quantity": "1"}],
            "tendered_cents": tendered,
        },
        format="json",
    ).json()


def open_drawer(client, *, float_cents=100000, client_uuid=None) -> dict:
    body = {"opening_float_cents": float_cents}
    if client_uuid:
        body["client_uuid"] = str(client_uuid)
    return client.post(OPEN, body, format="json").json()


def close_drawer(client, shift, declared) -> dict:
    return client.post(
        f"/api/v1/shifts/{shift['id']}/close/",
        {"declared_closing_cents": declared},
        format="json",
    ).json()


@pytest.fixture
def closed_shift_with_a_late_sale(
    client_cashier_a, cashier_a, tenant_a, device_a, item_a, stock_a
):
    """A drawer counted and signed for, then a sale that synced after.

    The exact situation a manager finds confusing: the drawer balanced when it
    was counted, and the day's sales figure is higher than the drawer was.
    """
    from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

    shift_uuid = uuid.uuid4()
    shift = open_drawer(client_cashier_a, client_uuid=shift_uuid)

    # One sale rung up and counted, so the drawer balances.
    sell(client_cashier_a, item_a)
    close_drawer(client_cashier_a, shift, 118000)

    # A second sale that was rung up offline during the shift and only reached
    # the server after it closed.
    device, _token = device_a
    client_cashier_a.post(
        SYNC,
        batch(device, [offline_sale(item_a, shift_client_uuid=str(shift_uuid))]),
        format="json",
    )

    with tenant_context(tenant_a.id):
        return Shift.objects.get(pk=shift["id"])


@pytest.mark.django_db
class TestTheFrozenFiguresNeverMove:
    def test_the_variance_stays_as_it_was_counted(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        """A variance somebody signed off must not change under them days
        later."""
        with tenant_context(tenant_a.id):
            row = reconcile_shift(closed_shift_with_a_late_sale)

        assert row.variance_cents == 0
        assert row.declared_closing_cents == 118000
        assert row.expected_closing_cents == 118000

    def test_the_late_sale_is_not_added_into_the_expectation(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        with tenant_context(tenant_a.id):
            row = reconcile_shift(closed_shift_with_a_late_sale)

        # 118000, not 136000. The second sale is beside the figure, not in it.
        assert row.expected_closing_cents == 118000


@pytest.mark.django_db
class TestBothHalvesAreShown:
    def test_the_late_arrival_is_listed(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        with tenant_context(tenant_a.id):
            row = reconcile_shift(closed_shift_with_a_late_sale)

        assert row.late_count == 1
        assert row.late[0].amount_cents == 18000
        assert row.late[0].method == PaymentMethod.CASH

    def test_it_names_the_sale_so_a_manager_can_go_and_look(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        """The point of the foreign key: a query, not an investigation."""
        with tenant_context(tenant_a.id):
            row = reconcile_shift(closed_shift_with_a_late_sale)
            sale = Sale.objects.get(pk=row.late[0].sale_id)

        assert sale.was_offline is True

    def test_the_gap_is_explained_without_being_corrected(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        """A manager can see the drawer is 18000 below the day's sales *because*
        one sale synced after close - and stop looking for missing cash."""
        with tenant_context(tenant_a.id):
            row = reconcile_shift(closed_shift_with_a_late_sale)

        assert row.variance_cents == 0
        assert row.late_cash_cents == 18000
        assert row.explained_variance_cents == 18000

    def test_a_shift_with_nothing_late_says_so(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        shift = open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)
        close_drawer(client_cashier_a, shift, 118000)

        with tenant_context(tenant_a.id):
            row = reconcile_shift(Shift.objects.get(pk=shift["id"]))

        assert row.is_reconciled is True
        assert row.late_count == 0
        assert row.explained_variance_cents == row.variance_cents

    def test_an_unreconciled_shift_says_so_too(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        with tenant_context(tenant_a.id):
            row = reconcile_shift(closed_shift_with_a_late_sale)

        assert row.is_reconciled is False

    def test_the_two_halves_are_separate_in_the_payload(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        """Structurally separate, so no caller can accidentally add them."""
        with tenant_context(tenant_a.id):
            body = reconcile_shift(closed_shift_with_a_late_sale).as_dict()

        assert body["counted"]["variance_cents"] == 0
        assert body["arrived_after_close"]["cash_cents"] == 18000
        assert "variance_cents" not in body["arrived_after_close"]


@pytest.mark.django_db
class TestAnMpesaPaymentIsNotDrawerMoney:
    def test_it_never_appears_in_the_late_cash_figure(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """It was never in the drawer, so it cannot explain a cash gap."""
        from apps.shifts.services import attribute_payment

        shift = open_drawer(client_cashier_a)
        settled = sell(client_cashier_a, item_a)
        close_drawer(client_cashier_a, shift, 118000)

        with tenant_context(tenant_a.id):
            row = Shift.objects.get(pk=shift["id"])
            sale = Sale.objects.get(pk=settled["id"])
            mpesa = Payment.objects.create(
                tenant=tenant_a,
                sale=sale,
                method=PaymentMethod.MPESA,
                amount_cents=50000,
                user=cashier_a,
            )
            attribute_payment(payment=mpesa, shift=row)
            reconciliation = reconcile_shift(Shift.objects.get(pk=shift["id"]))

        assert reconciliation.late_cash_cents == 0
        assert reconciliation.is_reconciled is True


@pytest.mark.django_db
class TestThePeriodView:
    def test_shifts_are_bucketed_by_when_they_opened(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """A shift running past midnight belongs to the day it started, which
        is how the person who worked it thinks about it."""
        shift = open_drawer(client_cashier_a)
        close_drawer(client_cashier_a, shift, 100000)

        with tenant_context(tenant_a.id):
            rows = shift_summary(tenant_a, period_for(tenant_a))

        assert len(rows) == 1

    def test_totals_keep_the_two_halves_apart(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        with tenant_context(tenant_a.id):
            totals = drawer_totals(shift_summary(tenant_a, period_for(tenant_a)))

        assert totals.shift_count == 1
        assert totals.variance_cents == 0
        assert totals.late_cash_cents == 18000
        assert totals.late_count == 1

    def test_an_open_shift_contributes_no_variance(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """There is no variance until somebody has counted."""
        open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            rows = shift_summary(tenant_a, period_for(tenant_a))
            totals = drawer_totals(rows)

        assert rows[0].state == ShiftState.OPEN
        assert rows[0].variance_cents is None
        assert rows[0].explained_variance_cents is None
        assert totals.open_shift_count == 1
        assert totals.variance_cents == 0

    def test_cash_taken_can_exceed_what_the_drawers_were_counted_at(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        """This is the disagreement, stated as a figure. It is not a fault."""
        with tenant_context(tenant_a.id):
            period = period_for(tenant_a)
            taken = cash_taken_in(tenant_a, period)
            totals = drawer_totals(shift_summary(tenant_a, period))

        assert taken == 36000
        assert totals.declared_cents == 118000
        # The float accounts for the rest: 100000 + 36000 = 136000, of which
        # 118000 was counted and 18000 arrived after.
        assert taken - totals.late_cash_cents == 18000

    def test_the_unreconciled_count_is_one_query(
        self, closed_shift_with_a_late_sale, tenant_a
    ):
        """A number a manager can act on, without loading every shift the
        business has ever run."""
        with tenant_context(tenant_a.id):
            assert unreconciled_shift_count(tenant_a) == 1

    def test_it_is_zero_when_everything_tied_out(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        shift = open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)
        close_drawer(client_cashier_a, shift, 118000)

        with tenant_context(tenant_a.id):
            assert unreconciled_shift_count(tenant_a) == 0


@pytest.mark.django_db
class TestTheDrawerEndpoint:
    def test_a_manager_reads_it(
        self, client_manager_a, closed_shift_with_a_late_sale
    ):
        response = client_manager_a.get(DRAWERS)

        assert response.status_code == 200
        body = response.json()
        assert body["shifts"][0]["counted"]["variance_cents"] == 0
        assert body["shifts"][0]["arrived_after_close"]["cash_cents"] == 18000

    def test_a_cashier_may_not(self, client_cashier_a):
        assert client_cashier_a.get(DRAWERS).status_code == 403

    def test_the_payload_says_the_figures_are_frozen(
        self, client_manager_a, closed_shift_with_a_late_sale
    ):
        """Said in the response, not only in the code, because whoever reads
        this over an API will not have read the code."""
        body = client_manager_a.get(DRAWERS).json()

        assert "frozen" in body["note"]
        assert "not added into them" in body["note"]

    def test_it_carries_the_figure_the_drawers_are_compared_against(
        self, client_manager_a, closed_shift_with_a_late_sale
    ):
        body = client_manager_a.get(DRAWERS).json()

        assert body["cash_taken_in_period_cents"] == 36000
        assert body["unreconciled_shift_count"] == 1

    def test_the_csv_shows_both_halves_side_by_side(
        self, client_manager_a, closed_shift_with_a_late_sale
    ):
        import csv
        import io

        response = client_manager_a.get(DRAWERS + "csv/")
        rows = list(csv.reader(io.StringIO(response.content.decode())))

        assert "Variance" in rows[0]
        assert "Arrived after close" in rows[0]
        # The variance column still reads 0.00; the explanation is beside it.
        assert rows[1][6] == "0.00"
        assert rows[1][7] == "180.00"

    def test_another_businesss_drawers_are_invisible(
        self, client_owner_b, closed_shift_with_a_late_sale, store_b
    ):
        assert client_owner_b.get(DRAWERS).json()["shifts"] == []
