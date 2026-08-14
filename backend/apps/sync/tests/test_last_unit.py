"""
Two disconnected tills selling the same last bag of sugar.

This is the scenario offline selling makes unavoidable. Two tills, one shelf,
no network between them: both cashiers see one unit in their cached catalogue,
both sell it, both take the money. By the time either syncs, the goods are gone
twice over and one of those customers walked out with something the shop did
not have.

The rule the whole subsystem is built on decides this: **a completed sale is a
fact, not a request.** Both sales are accepted. The stock goes negative, which
is surfaced for a person to reconcile rather than refused - because refusing the
second sale would leave the shop's books without cash the shop physically has,
and would not put the bag of sugar back on the shelf either.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.accounts.models import Device
from apps.core.tenancy import tenant_context
from apps.inventory.models import MovementReason, StockItem, apply_movement
from apps.sales.models import Sale, SaleDiscrepancy, SaleState
from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale


@pytest.fixture
def one_unit_left(tenant_a, item_a, store_a):
    """A single bag of sugar on the shelf."""
    with tenant_context(tenant_a.id):
        stock = StockItem.objects.create(tenant=tenant_a, item=item_a, store=store_a)
        apply_movement(
            stock_item=stock,
            delta=1,
            reason=MovementReason.PURCHASE,
            note="Last one",
        )
        stock.refresh_from_db()
        return stock


@pytest.fixture
def second_till(tenant_a):
    """A second counter, which was also offline."""
    with tenant_context(tenant_a.id):
        return Device.issue(tenant=tenant_a, name="Second counter")


def sell_the_last_unit(client, device, item):
    """One till emptying its outbox with a single sale in it."""
    return client.post(
        SYNC,
        batch(device, [offline_sale(item, device_created_at=timezone.now().isoformat())]),
        format="json",
    ).json()["results"][0]


@pytest.mark.django_db
class TestTwoTillsSoldTheSameLastUnit:
    def test_both_sales_are_accepted(
        self, client_cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        """Neither customer is told, hours later, that their purchase did not
        happen. The money is already in two drawers."""
        first_till, _token = device_a
        other_till, _other = second_till

        first = sell_the_last_unit(client_cashier_a, first_till, item_a)
        second = sell_the_last_unit(client_cashier_a, other_till, item_a)

        assert first["status"] == "accepted"
        assert second["status"] == "accepted"

    def test_both_sales_settle_and_take_a_receipt_number(
        self, client_cashier_a, cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        first_till, _token = device_a
        other_till, _other = second_till

        first = sell_the_last_unit(client_cashier_a, first_till, item_a)
        second = sell_the_last_unit(client_cashier_a, other_till, item_a)

        with tenant_context(cashier_a.tenant_id):
            sales = Sale.objects.filter(
                pk__in=[first["sale_id"], second["sale_id"]]
            )
            states = {sale.state for sale in sales}
            numbers = sorted(sale.receipt_number for sale in sales)

        assert states == {SaleState.PAID}
        assert numbers == [1, 2]

    def test_the_stock_goes_negative_rather_than_stopping_at_zero(
        self, client_cashier_a, cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        """Clamping at zero would hide the fact that a unit is unaccounted for,
        which is exactly the thing the shop needs to see."""
        first_till, _token = device_a
        other_till, _other = second_till

        sell_the_last_unit(client_cashier_a, first_till, item_a)
        sell_the_last_unit(client_cashier_a, other_till, item_a)

        with tenant_context(cashier_a.tenant_id):
            stock = StockItem.objects.get(pk=one_unit_left.pk)

        assert stock.quantity == -1

    def test_the_oversell_is_flagged_for_a_person(
        self, client_cashier_a, cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        first_till, _token = device_a
        other_till, _other = second_till

        sell_the_last_unit(client_cashier_a, first_till, item_a)
        second = sell_the_last_unit(client_cashier_a, other_till, item_a)

        with tenant_context(cashier_a.tenant_id):
            discrepancies = SaleDiscrepancy.objects.filter(
                kind=SaleDiscrepancy.Kind.NEGATIVE_STOCK
            )

            # Only the second sale drove the count under. The first was a
            # perfectly ordinary sale of the last unit.
            assert discrepancies.count() == 1
            flagged = discrepancies.get()
            assert str(flagged.sale_id) == second["sale_id"]
            assert flagged.context["balance_after"] == "-1.000"
            assert flagged.is_open

    def test_each_till_is_recorded_against_its_own_sale(
        self, client_cashier_a, cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        """A shop reconciling this needs to know which counter took which
        payment, or it cannot go and count the two drawers."""
        first_till, _token = device_a
        other_till, _other = second_till

        first = sell_the_last_unit(client_cashier_a, first_till, item_a)
        second = sell_the_last_unit(client_cashier_a, other_till, item_a)

        with tenant_context(cashier_a.tenant_id):
            first_sale = Sale.objects.get(pk=first["sale_id"])
            second_sale = Sale.objects.get(pk=second["sale_id"])

        assert first_sale.device_id == first_till.id
        assert second_sale.device_id == other_till.id
        assert first_sale.was_offline and second_sale.was_offline

    def test_the_money_from_both_sales_is_kept(
        self, client_cashier_a, cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        """The point of accepting the oversell. Refusing the second sale would
        leave the books short by cash that is physically in the drawer."""
        first_till, _token = device_a
        other_till, _other = second_till

        first = sell_the_last_unit(client_cashier_a, first_till, item_a)
        second = sell_the_last_unit(client_cashier_a, other_till, item_a)

        with tenant_context(cashier_a.tenant_id):
            taken = sum(
                payment.amount_cents
                for sale_id in (first["sale_id"], second["sale_id"])
                for payment in Sale.objects.get(pk=sale_id).payments.all()
            )

        assert taken == 36000

    def test_replaying_either_batch_does_not_oversell_further(
        self, client_cashier_a, cashier_a, device_a, second_till, item_a, one_unit_left
    ):
        """The connection that came back is the one that drops again. A second
        upload of the same two sales must not take the count to -3."""
        first_till, _token = device_a
        other_till, _other = second_till

        first_batch = batch(first_till, [offline_sale(item_a)])
        second_batch = batch(other_till, [offline_sale(item_a)])

        client_cashier_a.post(SYNC, first_batch, format="json")
        client_cashier_a.post(SYNC, second_batch, format="json")
        replay = client_cashier_a.post(SYNC, first_batch, format="json").json()

        assert replay["results"][0]["status"] == "duplicate"
        with tenant_context(cashier_a.tenant_id):
            assert StockItem.objects.get(pk=one_unit_left.pk).quantity == -1
            assert Sale.objects.count() == 2
