"""
A weighed line that came to almost nothing.

The discount gate cannot catch this. A weighed item's price is not
client-supplied - the catalogue rate is - so there is nothing for that gate to
check. The *quantity* is client-supplied, and understating it reaches the same
place by a different road: ring up 0.001 kg of sugar for eighteen cents and hand
over a kilo.

These pin that an ordinary 200g purchase never trips it, and that the 0.001 kg
case does.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import override_settings

from apps.catalog.models import Item, UnitOfMeasure
from apps.core.tenancy import tenant_context
from apps.inventory.models import MovementReason, StockItem, apply_movement
from apps.sales.models import SaleDiscrepancy, SaleState

CHECKOUT = "/api/v1/sales/checkout/cash/"


@pytest.fixture
def sugar_by_the_kilo(tenant_a, tax_rate_a, store_a):
    """Sugar at KES 180 a kilo, with forty kilos on the shelf."""
    with tenant_context(tenant_a.id):
        item = Item.objects.create(
            tenant=tenant_a,
            sku="SUGAR-LOOSE",
            name="Sugar (loose)",
            price_cents=18000,
            unit=UnitOfMeasure.KILOGRAM,
            tax_rate=tax_rate_a,
        )
        stock = StockItem.objects.create(tenant=tenant_a, item=item, store=store_a)
        apply_movement(
            stock_item=stock,
            delta=Decimal("40"),
            reason=MovementReason.PURCHASE,
            note="A sack",
        )
        return item


def sell(client, item, *, quantity, tendered):
    return client.post(
        CHECKOUT,
        {
            "lines": [{"item_id": str(item.id), "quantity": quantity}],
            "tendered_cents": tendered,
        },
        format="json",
    ).json()


def flags(tenant_id):
    with tenant_context(tenant_id):
        return list(
            SaleDiscrepancy.objects.filter(
                kind=SaleDiscrepancy.Kind.SUSPICIOUS_QUANTITY
            )
        )


@pytest.mark.django_db
class TestAnOrdinaryWeighedSale:
    def test_two_hundred_grams_never_trips_it(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        """The case that must not be flagged. 0.2 kg at 180 a kilo is KES 36 -
        an entirely normal purchase, and a shop that got a warning for it would
        stop reading the warnings within a day."""
        settled = sell(client_cashier_a, sugar_by_the_kilo, quantity="0.2", tendered=3600)

        assert settled["state"] == SaleState.PAID
        assert flags(cashier_a.tenant_id) == []

    def test_a_whole_kilo_never_trips_it(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        sell(client_cashier_a, sugar_by_the_kilo, quantity="1", tendered=18000)

        assert flags(cashier_a.tenant_id) == []

    def test_a_quantity_right_on_the_floor_does_not_trip_it(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        """The comparison is strictly below, so exactly the threshold passes.
        A boundary that flagged its own limit would flag a legitimate sale of
        precisely that value every time."""
        # 2000 cents at 18000 a kilo is 0.111... kg; 0.112 comes to 2016.
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.112", tendered=2016)

        assert flags(cashier_a.tenant_id) == []


@pytest.mark.django_db
class TestAQuantityThatCameToNothing:
    def test_a_thousandth_of_a_kilo_is_flagged(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        """Ten grams of sugar - KES 1.80 - and a kilo over the counter."""
        settled = sell(
            client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200
        )

        raised = flags(cashier_a.tenant_id)
        assert len(raised) == 1
        assert str(raised[0].sale_id) == settled["id"]

    def test_the_sale_still_completes(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        """A compensating control, not a gate. Blocking it would stop a
        legitimate tiny sale and would not stop a determined one."""
        settled = sell(
            client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200
        )

        assert settled["state"] == SaleState.PAID
        assert settled["receipt_number"] is not None

    def test_the_cashier_is_asked_for_nothing(
        self, client_cashier_a, sugar_by_the_kilo
    ):
        """No reason, no manager, no friction at the counter. It is visible
        afterwards, the same way an offline shortfall is."""
        response = client_cashier_a.post(
            CHECKOUT,
            {
                "lines": [
                    {"item_id": str(sugar_by_the_kilo.id), "quantity": "0.01"}
                ],
                "tendered_cents": 200,
            },
            format="json",
        )

        assert response.status_code == 201

    def test_the_flag_carries_what_a_person_needs(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200)

        context = flags(cashier_a.tenant_id)[0].context
        assert context["quantity"] == "0.010"
        assert context["unit"] == UnitOfMeasure.KILOGRAM
        assert context["unit_price_cents"] == 18000
        assert context["floor_cents"] == 2000

    def test_the_detail_reads_as_a_sentence(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200)

        detail = flags(cashier_a.tenant_id)[0].detail
        assert "Sugar (loose)" in detail
        assert "worth a look" in detail

    def test_it_is_left_open_for_somebody_to_resolve(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200)

        assert flags(cashier_a.tenant_id)[0].is_open


@pytest.mark.django_db
class TestOnlyMeasuredItems:
    def test_a_cheap_item_sold_each_is_not_flagged(
        self, client_cashier_a, cashier_a, tenant_a, tax_rate_a, store_a
    ):
        """A single sweet is an ordinary sale. Flagging every cheap thing would
        bury the signal in noise within a day."""
        with tenant_context(tenant_a.id):
            sweet = Item.objects.create(
                tenant=tenant_a,
                sku="SWEET",
                name="Sweet",
                price_cents=200,
                unit=UnitOfMeasure.EACH,
                tax_rate=tax_rate_a,
                track_stock=False,
            )

        sell(client_cashier_a, sweet, quantity="1", tendered=200)

        assert flags(tenant_a.id) == []

    def test_a_litre_measure_is_covered_too(
        self, client_cashier_a, cashier_a, tenant_a, tax_rate_a, store_a
    ):
        """Not only weight. Cooking oil from a drum has the same shape."""
        with tenant_context(tenant_a.id):
            oil = Item.objects.create(
                tenant=tenant_a,
                sku="OIL-LOOSE",
                name="Cooking oil (loose)",
                price_cents=30000,
                unit=UnitOfMeasure.LITRE,
                tax_rate=tax_rate_a,
                track_stock=False,
            )

        sell(client_cashier_a, oil, quantity="0.01", tendered=300)

        assert len(flags(tenant_a.id)) == 1


@pytest.mark.django_db
class TestTheFloorIsTunable:
    @override_settings(SUSPICIOUS_QUANTITY_FLOOR_CENTS=10000)
    def test_raising_it_catches_more(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        # 0.2 kg is KES 36, under a KES 100 floor.
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.2", tendered=3600)

        assert len(flags(cashier_a.tenant_id)) == 1

    @override_settings(SUSPICIOUS_QUANTITY_FLOOR_CENTS=0)
    def test_zero_turns_it_off(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        """A shop that genuinely sells grams of something can switch it off
        rather than learning to ignore it."""
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200)

        assert flags(cashier_a.tenant_id) == []


@pytest.mark.django_db
class TestWhenItIsRaised:
    def test_an_unsettled_sale_raises_nothing(
        self, tenant_a, store_a, cashier_a, sugar_by_the_kilo
    ):
        """An abandoned cart is not a sale."""
        from apps.sales.services import LineRequest, create_sale

        with tenant_context(tenant_a.id):
            create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[
                    LineRequest(
                        item_id=str(sugar_by_the_kilo.id), quantity=Decimal("0.001")
                    )
                ],
            )

        assert flags(tenant_a.id) == []

    def test_it_is_raised_once_not_on_every_recompute(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        """Settlement happens once; a second call must not double the flag."""
        settled = sell(
            client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200
        )

        from apps.sales.models import Sale
        from apps.sales.services import recompute_state

        with tenant_context(cashier_a.tenant_id):
            recompute_state(Sale.objects.get(pk=settled["id"]))

        assert len(flags(cashier_a.tenant_id)) == 1

    def test_a_synced_offline_sale_is_flagged_too(
        self, client_cashier_a, cashier_a, device_a, sugar_by_the_kilo
    ):
        """The likeliest place for it, since nobody was watching the counter."""
        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        device, _token = device_a
        payload = offline_sale(sugar_by_the_kilo, tendered=200)
        payload["lines"] = [
            {"item_id": str(sugar_by_the_kilo.id), "quantity": "0.01"}
        ]

        client_cashier_a.post(SYNC, batch(device, [payload]), format="json")

        assert len(flags(cashier_a.tenant_id)) == 1


@pytest.mark.django_db
class TestASaleThatRoundsToNothing:
    """Found while writing the tests above.

    A thousandth of a kilo of sugar is eighteen cents, and cash rounds to the
    shilling - so the sale came to zero, and the payment ledger's
    positive-amount constraint rejected it as an integrity error. A cashier saw
    a conflict with no explanation.
    """

    def test_it_is_refused_with_something_readable(
        self, client_cashier_a, sugar_by_the_kilo
    ):
        response = client_cashier_a.post(
            CHECKOUT,
            {
                "lines": [
                    {"item_id": str(sugar_by_the_kilo.id), "quantity": "0.001"}
                ],
                "tendered_cents": 100,
            },
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "rounds_to_nothing"
        assert "quantity" in response.json()["detail"]

    def test_it_leaves_no_sale_behind(
        self, client_cashier_a, cashier_a, sugar_by_the_kilo
    ):
        from apps.sales.models import Sale

        client_cashier_a.post(
            CHECKOUT,
            {
                "lines": [
                    {"item_id": str(sugar_by_the_kilo.id), "quantity": "0.001"}
                ],
                "tendered_cents": 100,
            },
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 0


@pytest.mark.django_db
class TestItStaysInsideOneBusiness:
    def test_another_business_does_not_see_the_flag(
        self, client_cashier_a, cashier_a, tenant_b, sugar_by_the_kilo
    ):
        sell(client_cashier_a, sugar_by_the_kilo, quantity="0.01", tendered=200)

        with tenant_context(tenant_b.id):
            assert (
                SaleDiscrepancy.objects.filter(
                    kind=SaleDiscrepancy.Kind.SUSPICIOUS_QUANTITY
                ).count()
                == 0
            )
