"""
The guards that stop money leaving through the wrong door.

Every test here is a negative path. Each corresponds to something a person
holding a till could try, and the point of the file is that the *service* holds
the line rather than relying on a view remembering to check - because by the end
of this milestone there are three ways into a sale: the till, the sync endpoint,
and an M-Pesa callback.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from apps.catalog.models import Item
from apps.core.tenancy import tenant_context
from apps.sales.models import Sale, SaleState
from apps.sales.services import (
    CheckoutError,
    LineRequest,
    create_sale,
    ledger_position,
    take_cash,
    void_sale,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def variable_item(tenant_a, tax_rate_a):
    """A service quoted on the day: 'Braiding, from KES 500'."""
    with transaction.atomic(), tenant_context(tenant_a.id):
        return Item.objects.create(
            tenant=tenant_a,
            sku="SVC-BRAID",
            name="Braiding",
            price_cents=50000,
            item_type="SERVICE",
            track_stock=False,
            is_price_variable=True,
            tax_rate=tax_rate_a,
        )


def build(tenant, store, cashier, **kwargs):
    return create_sale(tenant=tenant, store=store, cashier=cashier, **kwargs)


class TestClientSuppliedPricesAreIgnored:
    """The catalogue decides the price of an ordinary item. Always."""

    def test_a_supplied_price_is_ignored_on_a_fixed_price_item(
        self, tenant_a, store_a, cashier_a, item_a
    ):
        """The fraud this prevents: sell a KES 180 bag of sugar for one shilling.

        Not an error - the field is simply not consulted - so a till that sends
        a stale cached price cannot fail a sale either. The catalogue is the only
        source.
        """
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[
                    LineRequest(
                        item_id=str(item_a.id),
                        quantity=Decimal("1"),
                        unit_price_cents=100,  # a cashier claiming KES 1
                    )
                ],
            )
            line = sale.lines.get()

        assert line.unit_price_cents == 18000
        assert sale.total_cents == 18000

    def test_a_supplied_price_above_the_catalogue_is_also_ignored(
        self, tenant_a, store_a, cashier_a, item_a
    ):
        """Overcharging is refused by the same rule, not a different one."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[
                    LineRequest(
                        item_id=str(item_a.id),
                        quantity=Decimal("1"),
                        unit_price_cents=99900,
                    )
                ],
            )

        assert sale.total_cents == 18000

    def test_an_item_from_another_business_is_refused(
        self, tenant_a, store_a, cashier_a, item_b
    ):
        """Isolation, at the service rather than only the view."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(CheckoutError) as raised:
                build(
                    tenant_a,
                    store_a,
                    cashier_a,
                    lines=[LineRequest(item_id=str(item_b.id), quantity=Decimal("1"))],
                )

        assert raised.value.code == "unknown_item"


class TestVariablePricesHaveAFloor:
    """``price_cents`` is a minimum, not a suggestion.

    Decided rather than left open: "from KES 500" means the price rises for a
    bigger job. Selling *below* the marked price is a discount - an auditable
    act with an authoriser and a reason - and giving that outcome a second,
    untrailed route is precisely what a dishonest cashier would use.
    """

    def test_the_marked_price_is_the_floor(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(CheckoutError) as raised:
                build(
                    tenant_a,
                    store_a,
                    cashier_a,
                    lines=[
                        LineRequest(
                            item_id=str(variable_item.id),
                            quantity=Decimal("1"),
                            unit_price_cents=5000,  # KES 50 for a KES 500 job
                        )
                    ],
                )

        assert raised.value.code == "below_minimum_price"
        assert "discount" in raised.value.detail

    def test_one_cent_below_the_floor_is_still_below_it(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(CheckoutError):
                build(
                    tenant_a,
                    store_a,
                    cashier_a,
                    lines=[
                        LineRequest(
                            item_id=str(variable_item.id),
                            quantity=Decimal("1"),
                            unit_price_cents=49999,
                        )
                    ],
                )

    def test_the_floor_itself_is_accepted(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[
                    LineRequest(
                        item_id=str(variable_item.id),
                        quantity=Decimal("1"),
                        unit_price_cents=50000,
                    )
                ],
            )

        assert sale.total_cents == 50000

    def test_above_the_floor_is_the_whole_point(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        """A bigger job costs more. That is what 'from' means."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[
                    LineRequest(
                        item_id=str(variable_item.id),
                        quantity=Decimal("1"),
                        unit_price_cents=90000,
                    )
                ],
            )

        assert sale.total_cents == 90000

    def test_omitting_a_price_falls_back_to_the_marked_one(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[LineRequest(item_id=str(variable_item.id), quantity=Decimal("1"))],
            )

        assert sale.total_cents == 50000

    def test_a_negative_price_is_refused(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(CheckoutError) as raised:
                build(
                    tenant_a,
                    store_a,
                    cashier_a,
                    lines=[
                        LineRequest(
                            item_id=str(variable_item.id),
                            quantity=Decimal("1"),
                            unit_price_cents=-100,
                        )
                    ],
                )

        assert raised.value.code == "bad_price"

    def test_a_discount_is_how_you_go_below_the_floor(
        self, tenant_a, store_a, cashier_a, variable_item
    ):
        """The sanctioned route, so the rule blocks nothing legitimate.

        Selling a damaged item cheap stays possible - it just leaves a discount
        on the record rather than an unexplained low price.
        """
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[
                    LineRequest(
                        item_id=str(variable_item.id),
                        quantity=Decimal("1"),
                        discount_cents=20000,
                    )
                ],
            )
            line = sale.lines.get()

        assert sale.total_cents == 30000
        assert line.line_discount_cents == 20000  # visible, not hidden in the price


class TestVoidChecksTheLedgerNotJustTheState:
    """The guard that must hold even when the cached state lies.

    The state machine and the ledger answer different questions. The machine
    knows what the cached state permits; the ledger knows whether money actually
    arrived. If the cache were ever wrong - a bug, a partial write, someone
    editing the database - a void that trusted only the machine would erase a
    settled sale.
    """

    def _paid_sale(self, tenant, store, cashier, item) -> Sale:
        with transaction.atomic(), tenant_context(tenant.id):
            sale = build(
                tenant,
                store,
                cashier,
                lines=[LineRequest(item_id=str(item.id), quantity=Decimal("1"))],
            )
            take_cash(sale=sale, tendered_cents=20000, user=cashier)
            sale.refresh_from_db()
            return sale

    def test_a_paid_sale_cannot_be_voided(
        self, tenant_a, store_a, cashier_a, item_a, manager_a
    ):
        sale = self._paid_sale(tenant_a, store_a, cashier_a, item_a)
        assert sale.state == SaleState.PAID

        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(Exception) as raised:
                void_sale(sale=sale, user=manager_a, reason="Changed my mind")

        assert "void" in str(raised.value).lower() or "refund" in str(raised.value).lower()

    def test_the_ledger_check_holds_when_the_cached_state_is_forced_back(
        self, tenant_a, store_a, cashier_a, item_a, manager_a
    ):
        """Force the cache to disagree, then try to void.

        The state machine would now permit OPEN -> VOID. The ledger still shows
        the money, and that is what refuses. Written this way because a guard
        that only works while the cache is correct is not a guard against the
        case where something has gone wrong.
        """
        sale = self._paid_sale(tenant_a, store_a, cashier_a, item_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            # Straight to the database, bypassing recompute_state entirely.
            Sale.objects.filter(pk=sale.pk).update(state=SaleState.OPEN)
            sale.refresh_from_db()
            assert sale.state == SaleState.OPEN

            position = ledger_position(sale)
            assert position.paid_cents == 18000  # the money is still there

            with pytest.raises(CheckoutError) as raised:
                void_sale(sale=sale, user=manager_a, reason="Trying it on")

        assert raised.value.code == "sale_already_paid"

    def test_the_sale_is_not_voided_by_the_failed_attempt(
        self, tenant_a, store_a, cashier_a, item_a, manager_a
    ):
        sale = self._paid_sale(tenant_a, store_a, cashier_a, item_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            Sale.objects.filter(pk=sale.pk).update(state=SaleState.OPEN)
            sale.refresh_from_db()
            with pytest.raises(CheckoutError):
                void_sale(sale=sale, user=manager_a, reason="Trying it on")

            sale.refresh_from_db()

        assert sale.state != SaleState.VOID
        assert sale.void_reason == ""

    def test_an_unpaid_sale_still_voids_normally(
        self, tenant_a, store_a, cashier_a, item_a, manager_a
    ):
        """The guard must not block the legitimate case."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=Decimal("1"))],
            )
            void_sale(sale=sale, user=manager_a, reason="Customer walked away")
            sale.refresh_from_db()

        assert sale.state == SaleState.VOID
        assert sale.void_reason == "Customer walked away"

    def test_a_void_needs_a_reason(self, tenant_a, store_a, cashier_a, item_a, manager_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=Decimal("1"))],
            )
            with pytest.raises(CheckoutError) as raised:
                void_sale(sale=sale, user=manager_a, reason="   ")

        assert raised.value.code == "reason_required"


class TestOtherServiceGuards:
    def test_an_empty_sale_is_refused(self, tenant_a, store_a, cashier_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(CheckoutError) as raised:
                build(tenant_a, store_a, cashier_a, lines=[])

        assert raised.value.code == "empty_sale"

    def test_a_zero_quantity_is_refused(self, tenant_a, store_a, cashier_a, item_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(CheckoutError) as raised:
                build(
                    tenant_a,
                    store_a,
                    cashier_a,
                    lines=[LineRequest(item_id=str(item_a.id), quantity=Decimal("0"))],
                )

        assert raised.value.code == "bad_quantity"

    def test_an_unavailable_item_cannot_be_sold(
        self, tenant_a, store_a, cashier_a, item_a
    ):
        """'Off today' has to actually stop it being rung up."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            item_a.is_available = False
            item_a.save(update_fields=["is_available"])

            with pytest.raises(CheckoutError) as raised:
                build(
                    tenant_a,
                    store_a,
                    cashier_a,
                    lines=[LineRequest(item_id=str(item_a.id), quantity=Decimal("1"))],
                )

        assert raised.value.code == "item_unavailable"

    def test_short_payment_is_refused(self, tenant_a, store_a, cashier_a, item_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = build(
                tenant_a,
                store_a,
                cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=Decimal("1"))],
            )
            with pytest.raises(CheckoutError) as raised:
                take_cash(sale=sale, tendered_cents=1000, user=cashier_a)

        assert raised.value.code == "insufficient_tender"
