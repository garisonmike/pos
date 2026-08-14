"""
Opening a drawer, counting it, and closing it.

The controls being tested here are the ones that make a count mean something: a
cashier who cannot see the expected figure, a variance that is recorded rather
than argued with, and closing figures that never move afterwards.
"""

from __future__ import annotations

import pytest

from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context
from apps.sales.models import Payment, PaymentMethod
from apps.shifts.models import (
    CashMovement,
    CashMovementKind,
    DrawerCount,
    Shift,
    ShiftDiscrepancy,
    ShiftState,
)
from apps.shifts.services import ShiftError, close_shift, drawer_position, open_shift

SHIFTS = "/api/v1/shifts/"
OPEN = "/api/v1/shifts/open/"
CURRENT = "/api/v1/shifts/current/"
CHECKOUT = "/api/v1/sales/checkout/cash/"


def close_url(shift) -> str:
    return f"/api/v1/shifts/{shift['id']}/close/"


def cash_url(shift) -> str:
    return f"/api/v1/shifts/{shift['id']}/cash/"


def open_drawer(client, *, float_cents=100000) -> dict:
    return client.post(
        OPEN, {"opening_float_cents": float_cents}, format="json"
    ).json()


def sell(client, item, *, tendered=18000) -> dict:
    return client.post(
        CHECKOUT,
        {
            "lines": [{"item_id": str(item.id), "quantity": "1"}],
            "tendered_cents": tendered,
        },
        format="json",
    ).json()


@pytest.mark.django_db
class TestOpeningADrawer:
    def test_a_cashier_opens_a_drawer_with_a_counted_float(
        self, client_cashier_a, cashier_a, store_a
    ):
        response = client_cashier_a.post(
            OPEN, {"opening_float_cents": 100000, "note": "Morning"}, format="json"
        )

        assert response.status_code == 201
        body = response.json()
        assert body["state"] == ShiftState.OPEN
        assert body["opening_float_cents"] == 100000
        assert body["cashier_username"] == "mary"

    def test_the_open_drawer_is_findable(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)

        current = client_cashier_a.get(CURRENT).json()

        assert current["shift"]["id"] == opened["id"]

    def test_nothing_is_open_before_one_is_started(self, client_cashier_a, store_a):
        assert client_cashier_a.get(CURRENT).json()["shift"] is None

    def test_a_second_drawer_for_the_same_person_is_refused(
        self, client_cashier_a, store_a
    ):
        """Two open drawers would make 'which drawer did this sale go into'
        unanswerable, and that answer is the whole reason the record exists."""
        open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            OPEN, {"opening_float_cents": 50000}, format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "shift_already_open"

    def test_two_people_may_each_have_a_drawer(
        self, client_cashier_a, client_manager_a, store_a
    ):
        assert client_cashier_a.post(
            OPEN, {"opening_float_cents": 100000}, format="json"
        ).status_code == 201
        assert client_manager_a.post(
            OPEN, {"opening_float_cents": 100000}, format="json"
        ).status_code == 201

    def test_a_negative_float_is_refused(self, client_cashier_a, store_a):
        response = client_cashier_a.post(
            OPEN, {"opening_float_cents": -1}, format="json"
        )
        assert response.status_code == 400

    def test_opening_is_audited(self, client_cashier_a, cashier_a, store_a):
        open_drawer(client_cashier_a)

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.SHIFT_OPENED)

        assert entry.after["opening_float_cents"] == 100000
        assert entry.actor_id == cashier_a.id

    def test_a_till_from_another_business_cannot_be_named(
        self, client_cashier_a, store_a, tenant_b
    ):
        from apps.accounts.models import Device

        with tenant_context(tenant_b.id):
            other_device, _token = Device.issue(tenant=tenant_b, name="Theirs")

        response = client_cashier_a.post(
            OPEN,
            {"opening_float_cents": 100000, "device_id": str(other_device.id)},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unknown_device"


@pytest.mark.django_db
class TestTheCountIsBlind:
    """A cashier who is shown the expected figure first is not counting, they
    are typing a number back. That makes the control theatre, which is worse
    than no control because it looks like one."""

    def test_an_open_drawer_never_reports_what_it_expects(
        self, client_cashier_a, item_a, stock_a
    ):
        opened = open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)

        current = client_cashier_a.get(CURRENT).json()["shift"]
        detail = client_cashier_a.get(f"{SHIFTS}{opened['id']}/").json()
        listing = client_cashier_a.get(SHIFTS).json()

        assert current["expected_closing_cents"] is None
        assert detail["expected_closing_cents"] is None
        assert listing["results"][0]["expected_closing_cents"] is None

    def test_an_open_drawer_never_reports_a_variance(
        self, client_cashier_a, item_a, stock_a
    ):
        open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)

        assert client_cashier_a.get(CURRENT).json()["shift"]["variance_cents"] is None

    def test_the_expected_figure_appears_only_once_the_count_is_in(
        self, client_cashier_a, item_a, stock_a
    ):
        opened = open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)

        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 118000}, format="json"
        ).json()

        assert closed["expected_closing_cents"] == 118000
        assert closed["variance_cents"] == 0


@pytest.mark.django_db
class TestWhatTheDrawerShouldHold:
    def test_float_plus_cash_sales(self, client_cashier_a, item_a, stock_a):
        opened = open_drawer(client_cashier_a, float_cents=100000)
        sell(client_cashier_a, item_a)
        sell(client_cashier_a, item_a)

        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 136000}, format="json"
        ).json()

        assert closed["expected_closing_cents"] == 136000

    def test_mpesa_never_enters_the_expected_cash(
        self, client_cashier_a, cashier_a, item_a, stock_a, store_a
    ):
        """A drawer holds cash. Folding mobile money in is how a till reads
        twenty thousand short every day until nobody trusts it."""
        opened = open_drawer(client_cashier_a)
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            shift = Shift.objects.get(pk=opened["id"])
            sale = Payment.objects.get(sale_id=settled["id"]).sale
            # An M-Pesa payment filed against the same drawer.
            Payment.objects.create(
                tenant=shift.tenant,
                sale=sale,
                method=PaymentMethod.MPESA,
                amount_cents=50000,
                user=cashier_a,
                shift=shift,
            )
            position = drawer_position(shift)

        assert position.cash_sales_cents == 18000
        assert position.expected_cents == 118000

    def test_cash_paid_in_raises_the_expectation(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)

        client_cashier_a.post(
            cash_url(opened),
            {
                "kind": CashMovementKind.PAID_IN,
                "amount_cents": 20000,
                "reason": "Float top-up",
            },
            format="json",
        )
        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 120000}, format="json"
        ).json()

        assert closed["expected_closing_cents"] == 120000
        assert closed["variance_cents"] == 0

    def test_cash_paid_out_lowers_it(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)

        client_cashier_a.post(
            cash_url(opened),
            {
                "kind": CashMovementKind.PAID_OUT,
                "amount_cents": 30000,
                "reason": "Paid the milk man",
            },
            format="json",
        )
        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 70000}, format="json"
        ).json()

        assert closed["expected_closing_cents"] == 70000

    def test_a_drop_to_the_safe_lowers_it_too(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)

        client_cashier_a.post(
            cash_url(opened),
            {"kind": CashMovementKind.DROP, "amount_cents": 50000, "reason": "To the safe"},
            format="json",
        )
        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 50000}, format="json"
        ).json()

        assert closed["expected_closing_cents"] == 50000

    def test_a_cash_movement_needs_a_reason(self, client_cashier_a, store_a):
        """'Cash out' with no reason is indistinguishable from theft."""
        opened = open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            cash_url(opened),
            {"kind": CashMovementKind.PAID_OUT, "amount_cents": 30000, "reason": ""},
            format="json",
        )

        assert response.status_code == 400

    def test_a_cash_movement_is_audited(
        self, client_cashier_a, cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            cash_url(opened),
            {"kind": CashMovementKind.PAID_OUT, "amount_cents": 30000, "reason": "Milk"},
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.CASH_MOVEMENT)

        assert entry.after["amount_cents"] == 30000
        assert entry.reason == "Milk"

    def test_cash_cannot_move_through_a_closed_drawer(
        self, client_cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        response = client_cashier_a.post(
            cash_url(opened),
            {"kind": CashMovementKind.PAID_IN, "amount_cents": 1000, "reason": "Late"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "shift_not_open"


@pytest.mark.django_db
class TestTheVariance:
    def test_a_drawer_that_balances_records_no_discrepancy(
        self, client_cashier_a, cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)

        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        ).json()

        assert closed["variance_cents"] == 0
        with tenant_context(cashier_a.tenant_id):
            assert ShiftDiscrepancy.objects.count() == 0

    def test_a_short_drawer_is_recorded_and_still_closes(
        self, client_cashier_a, cashier_a, store_a
    ):
        """A shop that cannot close its till stops trading. The count is a fact
        to record, not something to argue with."""
        opened = open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 95000}, format="json"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == ShiftState.CLOSED
        assert body["variance_cents"] == -5000

        with tenant_context(cashier_a.tenant_id):
            discrepancy = ShiftDiscrepancy.objects.get(
                kind=ShiftDiscrepancy.Kind.VARIANCE
            )
        assert discrepancy.context["variance_cents"] == -5000
        assert discrepancy.is_open

    def test_an_over_drawer_is_recorded_too(
        self, client_cashier_a, cashier_a, store_a
    ):
        """Over is not good news - it means something was mis-rung, and the
        customer who was overcharged is the one who lost."""
        opened = open_drawer(client_cashier_a)

        closed = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 107000}, format="json"
        ).json()

        assert closed["variance_cents"] == 7000
        with tenant_context(cashier_a.tenant_id):
            assert ShiftDiscrepancy.objects.filter(
                kind=ShiftDiscrepancy.Kind.VARIANCE
            ).exists()

    def test_the_discrepancy_shows_where_the_money_should_have_come_from(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """A cashier who is short needs to see which line it should have come
        from, not just that it is missing."""
        opened = open_drawer(client_cashier_a)
        sell(client_cashier_a, item_a)

        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 110000}, format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            context = ShiftDiscrepancy.objects.get(
                kind=ShiftDiscrepancy.Kind.VARIANCE
            ).context

        assert context["opening_float_cents"] == 100000
        assert context["cash_sales_cents"] == 18000
        assert context["expected_cents"] == 118000
        assert context["variance_cents"] == -8000

    def test_closing_is_audited_with_both_figures(
        self, client_cashier_a, cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 95000}, format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.SHIFT_CLOSED)

        assert entry.after["declared_cents"] == 95000
        assert entry.after["expected_cents"] == 100000
        assert entry.after["variance_cents"] == -5000


@pytest.mark.django_db
class TestTheDenominationBreakdown:
    def test_a_breakdown_that_adds_up_is_kept(
        self, client_cashier_a, cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            close_url(opened),
            {
                "declared_closing_cents": 100000,
                "denominations": {"100000": 1},
            },
            format="json",
        )

        assert response.status_code == 200
        with tenant_context(cashier_a.tenant_id):
            assert DrawerCount.objects.get().quantity == 1

    def test_a_breakdown_that_does_not_add_up_is_refused(
        self, client_cashier_a, store_a
    ):
        """Picking a winner between the two figures would hide which one the
        cashier got wrong."""
        opened = open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            close_url(opened),
            {
                "declared_closing_cents": 100000,
                "denominations": {"50000": 1},
            },
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "count_does_not_add_up"

    def test_a_refused_breakdown_leaves_the_drawer_open(
        self, client_cashier_a, cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened),
            {"declared_closing_cents": 100000, "denominations": {"50000": 1}},
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            assert Shift.objects.get(pk=opened["id"]).state == ShiftState.OPEN
            assert DrawerCount.objects.count() == 0

    def test_a_breakdown_is_optional(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestClosingFiguresAreFrozen:
    """A figure a person put their name to has to mean the same thing next
    week as it did the day they signed it. Recomputing would mean two different
    correct answers exist for 'what was this shift's variance', depending on
    when you ask."""

    def test_a_drawer_cannot_be_closed_twice(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        response = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 90000}, format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "shift_not_open"

    def test_a_second_close_does_not_change_the_figures(
        self, client_cashier_a, cashier_a, store_a
    ):
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 1}, format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            shift = Shift.objects.get(pk=opened["id"])

        assert shift.declared_closing_cents == 100000
        assert shift.variance_cents == 0

    def test_there_is_no_way_to_reopen_a_drawer(self, client_cashier_a, store_a):
        """A correction is a new record, not an edit - the same discipline the
        payment and refund ledgers follow."""
        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        for verb in ("put", "patch", "delete"):
            response = getattr(client_cashier_a, verb)(
                f"{SHIFTS}{opened['id']}/", {}, format="json"
            )
            assert response.status_code in (403, 405)


@pytest.mark.django_db
class TestASaleArrivingAfterTheDrawerClosed:
    """The case the frozen-figures decision exists for."""

    def _closed_shift_and_late_sale(self, client, cashier, item):
        opened = open_drawer(client)
        client.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        # A sale filed against the drawer after the fact, as a sync replay does.
        with tenant_context(cashier.tenant_id):
            shift = Shift.objects.get(pk=opened["id"])
        settled = sell(client, item)
        with tenant_context(cashier.tenant_id):
            payment = Payment.objects.get(sale_id=settled["id"])
            from apps.shifts.services import attribute_payment

            attribute_payment(payment=payment, shift=shift)
        return opened, settled

    def test_the_closing_figures_do_not_move(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        opened, _sale = self._closed_shift_and_late_sale(
            client_cashier_a, cashier_a, item_a
        )

        with tenant_context(cashier_a.tenant_id):
            shift = Shift.objects.get(pk=opened["id"])

        assert shift.expected_closing_cents == 100000
        assert shift.declared_closing_cents == 100000
        assert shift.variance_cents == 0

    def test_the_late_arrival_is_recorded_against_the_shift(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        opened, sale = self._closed_shift_and_late_sale(
            client_cashier_a, cashier_a, item_a
        )

        with tenant_context(cashier_a.tenant_id):
            discrepancy = ShiftDiscrepancy.objects.get(
                kind=ShiftDiscrepancy.Kind.LATE_ATTRIBUTION
            )

        # The foreign key back to the shift is the point: joining frozen
        # figures to what arrived afterwards is a query, not an investigation.
        assert str(discrepancy.shift_id) == opened["id"]
        assert discrepancy.context["sale_id"] == sale["id"]
        assert discrepancy.context["amount_cents"] == 18000
        assert discrepancy.context["frozen_variance_cents"] == 0

    def test_the_payment_still_names_the_drawer_it_belonged_to(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        opened, sale = self._closed_shift_and_late_sale(
            client_cashier_a, cashier_a, item_a
        )

        with tenant_context(cashier_a.tenant_id):
            payment = Payment.objects.get(sale_id=sale["id"])

        assert str(payment.shift_id) == opened["id"]

    def test_an_mpesa_payment_landing_late_raises_nothing(
        self, client_cashier_a, cashier_a, item_a, stock_a, store_a
    ):
        """It never went into a drawer, so it has no bearing on the count.
        Flagging it would be noise about money that was never in the till."""
        from apps.shifts.services import attribute_payment

        opened = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            shift = Shift.objects.get(pk=opened["id"])
            sale = Payment.objects.get(sale_id=settled["id"]).sale
            mpesa = Payment.objects.create(
                tenant=shift.tenant,
                sale=sale,
                method=PaymentMethod.MPESA,
                amount_cents=18000,
                user=cashier_a,
            )
            attribute_payment(payment=mpesa, shift=shift)

            assert not ShiftDiscrepancy.objects.filter(
                kind=ShiftDiscrepancy.Kind.LATE_ATTRIBUTION
            ).exists()


@pytest.mark.django_db
class TestWhoMayCloseADrawer:
    def test_a_cashier_closes_their_own(self, client_cashier_a, store_a):
        opened = open_drawer(client_cashier_a)

        response = client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        assert response.status_code == 200

    def test_a_cashier_cannot_close_somebody_elses(
        self, client_cashier_a, client_manager_a, store_a
    ):
        """They would be putting a figure against another person's name."""
        managers_drawer = client_manager_a.post(
            OPEN, {"opening_float_cents": 100000}, format="json"
        ).json()

        response = client_cashier_a.post(
            close_url(managers_drawer), {"declared_closing_cents": 90000}, format="json"
        )

        assert response.status_code == 403
        assert response.json()["code"] == "not_your_shift"

    def test_a_manager_may_close_a_cashiers_drawer(
        self, client_cashier_a, client_manager_a, store_a
    ):
        """The cashier went home. Somebody has to be able to count it."""
        drawer = open_drawer(client_cashier_a)

        response = client_manager_a.post(
            close_url(drawer), {"declared_closing_cents": 100000}, format="json"
        )

        assert response.status_code == 200

    def test_a_manager_closing_it_is_recorded_as_such(
        self, client_cashier_a, client_manager_a, cashier_a, store_a
    ):
        """A normal thing that happens, and also exactly what it would look
        like if it were not."""
        drawer = open_drawer(client_cashier_a)
        client_manager_a.post(
            close_url(drawer), {"declared_closing_cents": 100000}, format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            discrepancy = ShiftDiscrepancy.objects.get(
                kind=ShiftDiscrepancy.Kind.FORCED_CLOSE
            )

        assert discrepancy.context["closed_by"] == "mngr"
        assert discrepancy.context["cashier"] == "mary"

    def test_closing_your_own_raises_no_forced_close(
        self, client_cashier_a, cashier_a, store_a
    ):
        drawer = open_drawer(client_cashier_a)
        client_cashier_a.post(
            close_url(drawer), {"declared_closing_cents": 100000}, format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            assert not ShiftDiscrepancy.objects.filter(
                kind=ShiftDiscrepancy.Kind.FORCED_CLOSE
            ).exists()


@pytest.mark.django_db
class TestShiftsAreOptional:
    def test_a_sale_with_no_open_drawer_still_settles(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """A duka with one person and one drawer may never open a shift, and
        it must go on selling exactly as before."""
        settled = sell(client_cashier_a, item_a)

        assert settled["state"] == "PAID"
        with tenant_context(cashier_a.tenant_id):
            assert Payment.objects.get(sale_id=settled["id"]).shift_id is None

    def test_a_sale_during_an_open_drawer_is_filed_against_it(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        opened = open_drawer(client_cashier_a)
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            payment = Payment.objects.get(sale_id=settled["id"])

        assert str(payment.shift_id) == opened["id"]


@pytest.mark.django_db
class TestServiceGuards:
    def test_closing_needs_a_non_negative_count(
        self, tenant_a, store_a, cashier_a
    ):
        with tenant_context(tenant_a.id):
            shift = open_shift(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                opening_float_cents=0,
            )
            with pytest.raises(ShiftError) as exc:
                close_shift(
                    shift=shift,
                    declared_closing_cents=-1,
                    closed_by=cashier_a,
                )
        assert exc.value.code == "negative_count"

    def test_a_movement_must_be_positive(self, tenant_a, store_a, cashier_a):
        from apps.shifts.services import record_cash_movement

        with tenant_context(tenant_a.id):
            shift = open_shift(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                opening_float_cents=0,
            )
            with pytest.raises(ShiftError) as exc:
                record_cash_movement(
                    shift=shift,
                    kind=CashMovementKind.PAID_IN,
                    amount_cents=0,
                    reason="Nothing",
                    user=cashier_a,
                )
        assert exc.value.code == "invalid_amount"

    def test_the_drawer_position_sums_the_rows_it_has(
        self, tenant_a, store_a, cashier_a
    ):
        with tenant_context(tenant_a.id):
            shift = open_shift(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                opening_float_cents=50000,
            )
            CashMovement.objects.create(
                tenant=tenant_a,
                shift=shift,
                kind=CashMovementKind.PAID_IN,
                amount_cents=10000,
                reason="Top-up",
                user=cashier_a,
            )
            CashMovement.objects.create(
                tenant=tenant_a,
                shift=shift,
                kind=CashMovementKind.PAID_OUT,
                amount_cents=3000,
                reason="Milk",
                user=cashier_a,
            )
            position = drawer_position(shift)

        assert position.expected_cents == 57000
        assert position.paid_in_cents == 10000
        assert position.paid_out_cents == 3000


@pytest.mark.django_db
class TestShiftsStayInsideOneBusiness:
    def test_a_till_sees_only_its_own_shifts(
        self, client_cashier_a, client_owner_b, store_a, store_b
    ):
        open_drawer(client_cashier_a)
        client_owner_b.post(OPEN, {"opening_float_cents": 500000}, format="json")

        listing = client_cashier_a.get(SHIFTS).json()

        assert listing["count"] == 1
        assert listing["results"][0]["opening_float_cents"] == 100000

    def test_another_businesss_drawer_cannot_be_closed(
        self, client_cashier_a, client_owner_b, store_a, store_b
    ):
        theirs = client_owner_b.post(
            OPEN, {"opening_float_cents": 500000}, format="json"
        ).json()

        response = client_cashier_a.post(
            close_url(theirs), {"declared_closing_cents": 0}, format="json"
        )

        assert response.status_code == 404

    def test_another_businesss_drawer_cannot_take_cash(
        self, client_cashier_a, client_owner_b, store_a, store_b
    ):
        theirs = client_owner_b.post(
            OPEN, {"opening_float_cents": 500000}, format="json"
        ).json()

        response = client_cashier_a.post(
            cash_url(theirs),
            {
                "kind": CashMovementKind.PAID_OUT,
                "amount_cents": 500000,
                "reason": "Mine now",
            },
            format="json",
        )

        assert response.status_code == 404

    def test_opening_a_drawer_needs_authentication(self, anon_client):
        assert anon_client.post(
            OPEN, {"opening_float_cents": 100000}, format="json"
        ).status_code == 401

    def test_one_open_shift_per_cashier_is_scoped_per_business(
        self, client_cashier_a, client_owner_b, store_a, store_b
    ):
        """Two businesses each having an open drawer is normal. The constraint
        is per cashier, and cashiers belong to one business."""
        assert client_cashier_a.post(
            OPEN, {"opening_float_cents": 100000}, format="json"
        ).status_code == 201
        assert client_owner_b.post(
            OPEN, {"opening_float_cents": 100000}, format="json"
        ).status_code == 201


@pytest.mark.django_db
class TestSyncNamesTheDrawer:
    """An offline till says which drawer it recorded a sale against, by the
    identifier it generated itself."""

    def test_a_synced_sale_is_filed_against_the_named_drawer(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        import uuid as uuid_module

        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        shift_uuid = str(uuid_module.uuid4())
        client_cashier_a.post(
            OPEN,
            {"opening_float_cents": 100000, "client_uuid": shift_uuid},
            format="json",
        )

        device, _token = device_a
        result = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a, shift_client_uuid=shift_uuid)]),
            format="json",
        ).json()["results"][0]

        with tenant_context(cashier_a.tenant_id):
            payment = Payment.objects.get(sale_id=result["sale_id"])
            shift = Shift.objects.get(client_uuid=shift_uuid)

        assert payment.shift_id == shift.id

    def test_a_sale_for_a_drawer_already_closed_is_flagged_late(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        import uuid as uuid_module

        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        shift_uuid = str(uuid_module.uuid4())
        opened = client_cashier_a.post(
            OPEN,
            {"opening_float_cents": 100000, "client_uuid": shift_uuid},
            format="json",
        ).json()
        client_cashier_a.post(
            close_url(opened), {"declared_closing_cents": 100000}, format="json"
        )

        device, _token = device_a
        result = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a, shift_client_uuid=shift_uuid)]),
            format="json",
        ).json()["results"][0]

        assert "late_attribution" in result["flags"]
        with tenant_context(cashier_a.tenant_id):
            shift = Shift.objects.get(client_uuid=shift_uuid)
            assert shift.variance_cents == 0
            assert ShiftDiscrepancy.objects.filter(
                kind=ShiftDiscrepancy.Kind.LATE_ATTRIBUTION, shift=shift
            ).exists()

    def test_another_businesss_shift_identifier_is_not_found(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, client_owner_b, store_b
    ):
        import uuid as uuid_module

        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        theirs = str(uuid_module.uuid4())
        client_owner_b.post(
            OPEN, {"opening_float_cents": 100000, "client_uuid": theirs}, format="json"
        )

        device, _token = device_a
        result = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a, shift_client_uuid=theirs)]),
            format="json",
        ).json()["results"][0]

        # The sale still lands - the money is real - but it is filed against no
        # drawer rather than against somebody else's.
        assert result["status"] == "accepted"
        with tenant_context(cashier_a.tenant_id):
            assert Payment.objects.get(sale_id=result["sale_id"]).shift_id is None
