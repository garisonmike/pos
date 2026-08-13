"""
Stock levels, the ledger behind them, and who may move them.

The rule this file exists to defend: no stock moves without a reason and an
author. Adjusting stock is how a theft gets covered up, so the role boundary and
the record of crossing it are tested together rather than separately.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context
from apps.inventory.models import (
    MovementReason,
    StockItem,
    StockMovement,
    apply_movement,
    rebuild_quantity,
)

pytestmark = pytest.mark.django_db

STOCK = "/api/v1/stock/"


class TestTheLedger:
    """Quantity is a cache of the movements; the movements are the truth."""

    def test_a_movement_updates_the_quantity(self, stock_a):
        assert stock_a.quantity == Decimal("40.000")

    def test_every_movement_records_the_balance_after_it(self, stock_a, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            apply_movement(
                stock_item=stock_a, delta=Decimal("-5"), reason=MovementReason.WASTAGE,
                note="Torn bag",
            )
            balances = list(
                StockMovement.objects.filter(stock_item=stock_a)
                .order_by("created_at")
                .values_list("balance_after", flat=True)
            )
        assert balances == [Decimal("40.000"), Decimal("35.000")]

    def test_the_quantity_can_be_rebuilt_from_the_ledger(self, stock_a, tenant_a):
        """The cached total is only ever a convenience.

        If it and the ledger ever disagree, the ledger wins - because it is the
        one that can explain itself.
        """
        with transaction.atomic(), tenant_context(tenant_a.id):
            apply_movement(
                stock_item=stock_a, delta=Decimal("-7.5"), reason=MovementReason.SALE
            )
            StockItem.objects.filter(pk=stock_a.pk).update(quantity=Decimal("999"))

            assert rebuild_quantity(stock_a) == Decimal("32.500")
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("32.500")

    def test_fractional_quantities_work(self, stock_a, tenant_a):
        """Sugar comes out of an open sack by weight, not in whole units."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            apply_movement(
                stock_item=stock_a, delta=Decimal("-2.750"), reason=MovementReason.SALE
            )
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("37.250")

    def test_a_movement_needing_a_reason_is_refused_without_one(self, stock_a, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            with pytest.raises(ValueError, match="note is required"):
                apply_movement(
                    stock_item=stock_a, delta=Decimal("-1"), reason=MovementReason.ADJUSTMENT
                )

    def test_a_sale_needs_no_note(self, stock_a, tenant_a):
        """It carries a sale reference instead, which says more than free text."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            movement = apply_movement(
                stock_item=stock_a,
                delta=Decimal("-1"),
                reason=MovementReason.SALE,
                ref_type="sales.Sale",
                ref_id="abc123",
            )
        assert movement.ref_id == "abc123"


class TestAdjustments:
    def test_a_manager_can_adjust_by_a_delta(self, client_manager_a, stock_a):
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "WASTAGE", "note": "Spoiled in the rain"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["quantity"] == "37.000"

    def test_a_manager_can_adjust_to_a_counted_figure(self, client_manager_a, stock_a):
        """Counting a shelf gives you a total, not a difference.

        Making the caller subtract is how off-by-one errors get into stock.
        """
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"new_quantity": "38", "reason": "COUNT", "note": "Monthly count"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["quantity"] == "38.000"

    def test_a_count_records_when_it_happened(self, client_manager_a, stock_a, tenant_a):
        client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"new_quantity": "38", "reason": "COUNT", "note": "Monthly count"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.last_counted_at is not None

    def test_an_adjustment_without_a_reason_is_refused(self, client_manager_a, stock_a):
        """The rule the whole ledger exists to enforce."""
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "ADJUSTMENT"},
            format="json",
        )
        assert response.status_code == 400
        assert "reason is required" in str(response.json())

    def test_a_blank_reason_does_not_count_as_a_reason(self, client_manager_a, stock_a):
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "ADJUSTMENT", "note": "   "},
            format="json",
        )
        assert response.status_code == 400

    def test_giving_both_a_delta_and_a_total_is_refused(self, client_manager_a, stock_a):
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "1", "new_quantity": "50", "reason": "COUNT", "note": "x"},
            format="json",
        )
        assert response.status_code == 400

    def test_giving_neither_is_refused(self, client_manager_a, stock_a):
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/", {"reason": "COUNT", "note": "x"}, format="json"
        )
        assert response.status_code == 400

    def test_a_sale_cannot_be_faked_through_an_adjustment(self, client_manager_a, stock_a):
        """Otherwise stock could be moved as if sold, with no sale behind it."""
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-1", "reason": "SALE"},
            format="json",
        )
        assert response.status_code == 400

    def test_stock_may_go_negative_and_says_so(self, client_manager_a, stock_a):
        """Refusing would mean refusing to record something that happened.

        A negative figure is wrong, but it is visibly wrong, which is better
        than books that quietly disagree with the drawer.
        """
        response = client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-45", "reason": "WASTAGE", "note": "Written off after flood"},
            format="json",
        )
        assert response.status_code == 200

        body = response.json()
        assert body["quantity"] == "-5.000"
        assert body["is_negative"] is True
        assert "negative stock" in body["warning"]

    def test_a_cashier_cannot_adjust_stock(self, client_cashier_a, stock_a):
        response = client_cashier_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "WASTAGE", "note": "x"},
            format="json",
        )
        assert response.status_code == 403

    def test_a_cashier_can_still_see_stock(self, client_cashier_a, stock_a):
        assert client_cashier_a.get(STOCK).status_code == 200

    def test_the_quantity_cannot_be_written_directly(self, client_manager_a, stock_a):
        """Every change must go through a movement, so every change has a why."""
        response = client_manager_a.patch(
            f"{STOCK}{stock_a.id}/", {"quantity": "999"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["quantity"] == "40.000"


class TestAdjustmentsAreAudited:
    def test_an_adjustment_records_who_what_and_why(
        self, client_manager_a, stock_a, tenant_a, manager_a
    ):
        client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "WASTAGE", "note": "Spoiled in the rain"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.STOCK_ADJUST).first()

        assert entry is not None
        assert entry.actor_id == manager_a.id
        assert entry.reason == "Spoiled in the rain"
        assert entry.before["quantity"] == "40.000"
        assert entry.after["quantity"] == "37.000"
        assert entry.after["movement_reason"] == "WASTAGE"

    def test_the_ledger_records_who_made_the_change(
        self, client_manager_a, stock_a, tenant_a, manager_a
    ):
        client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "WASTAGE", "note": "Spoiled"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            movement = StockMovement.objects.filter(reason=MovementReason.WASTAGE).first()

        assert movement.user_id == manager_a.id
        assert movement.note == "Spoiled"

    def test_the_ledger_is_readable_through_the_api(self, client_manager_a, stock_a):
        client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-3", "reason": "WASTAGE", "note": "Spoiled"},
            format="json",
        )
        response = client_manager_a.get(f"{STOCK}{stock_a.id}/movements/")
        assert response.status_code == 200

        rows = response.json()["results"]
        assert rows[0]["reason"] == "WASTAGE"
        assert rows[0]["balance_after"] == "37.000"


class TestLowStock:
    def test_an_item_at_or_below_its_level_appears(self, client_manager_a, stock_a):
        client_manager_a.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"new_quantity": "10", "reason": "COUNT", "note": "Counted"},
            format="json",
        )
        response = client_manager_a.get(f"{STOCK}low/")
        assert response.status_code == 200
        assert len(response.json()["results"]) == 1

    def test_an_item_above_its_level_does_not(self, client_manager_a, stock_a):
        assert client_manager_a.get(f"{STOCK}low/").json()["results"] == []

    def test_a_reorder_level_of_zero_means_do_not_warn(
        self, client_manager_a, stock_a, tenant_a
    ):
        """Otherwise every unconfigured item sits here and the list is ignored."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.reorder_level = Decimal("0")
            stock_a.quantity = Decimal("0")
            stock_a.save(update_fields=["reorder_level", "quantity"])

        assert client_manager_a.get(f"{STOCK}low/").json()["results"] == []


class TestTrackingSetup:
    def test_a_service_cannot_be_stock_tracked(self, client_manager_a, service_a, store_a):
        response = client_manager_a.post(
            STOCK,
            {"item": str(service_a.id), "store": str(store_a.id)},
            format="json",
        )
        assert response.status_code == 400
        assert "no shelf to count" in str(response.json())

    def test_an_opening_quantity_arrives_as_a_movement(
        self, client_manager_a, item_a, store_a, tenant_a
    ):
        """Even the first figure has to be explainable."""
        response = client_manager_a.post(
            STOCK,
            {
                "item": str(item_a.id),
                "store": str(store_a.id),
                "opening_quantity": "25",
                "reorder_level": "5",
            },
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["quantity"] == "25.000"

        with transaction.atomic(), tenant_context(tenant_a.id):
            movement = StockMovement.objects.first()
        assert movement.reason == MovementReason.COUNT
        assert movement.balance_after == Decimal("25.000")

    def test_tracking_the_same_item_twice_at_one_branch_is_refused(
        self, client_manager_a, stock_a, item_a, store_a
    ):
        response = client_manager_a.post(
            STOCK, {"item": str(item_a.id), "store": str(store_a.id)}, format="json"
        )
        assert response.status_code == 400

    def test_stock_is_held_per_branch(self, client_manager_a, item_a, store_a, tenant_a):
        """The seam that makes a second branch an insert, not a redesign."""
        from apps.stores.models import Store

        with transaction.atomic(), tenant_context(tenant_a.id):
            second = Store.objects.create(tenant=tenant_a, name="Branch two", code="TWO")

        for store, quantity in ((store_a, "10"), (second, "3")):
            client_manager_a.post(
                STOCK,
                {"item": str(item_a.id), "store": str(store.id), "opening_quantity": quantity},
                format="json",
            )

        rows = client_manager_a.get(f"{STOCK}?item={item_a.id}").json()["results"]
        assert {row["store_code"]: row["quantity"] for row in rows} == {
            "MAIN": "10.000",
            "TWO": "3.000",
        }


class TestStockIsolation:
    """Same bar as milestone 1, applied to the two new tables."""

    def test_stock_is_not_visible_across_businesses(self, client_owner_b, stock_a, stock_b):
        rows = client_owner_b.get(STOCK).json()["results"]
        assert [row["item_sku"] for row in rows] == ["NAILS-2IN"]

    def test_another_businesss_stock_cannot_be_read(self, client_owner_b, stock_a):
        assert client_owner_b.get(f"{STOCK}{stock_a.id}/").status_code == 404

    def test_another_businesss_stock_cannot_be_adjusted(
        self, client_owner_b, stock_a, tenant_a
    ):
        response = client_owner_b.post(
            f"{STOCK}{stock_a.id}/adjust/",
            {"delta": "-40", "reason": "WASTAGE", "note": "Not mine to touch"},
            format="json",
        )
        assert response.status_code == 404

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("40.000")

    def test_another_businesss_ledger_cannot_be_read(self, client_owner_b, stock_a):
        assert client_owner_b.get(f"{STOCK}{stock_a.id}/movements/").status_code == 404

    def test_stock_cannot_be_created_against_another_businesss_item(
        self, client_owner_b, item_a, store_b
    ):
        response = client_owner_b.post(
            STOCK, {"item": str(item_a.id), "store": str(store_b.id)}, format="json"
        )
        assert response.status_code == 400

    def test_stock_cannot_be_created_against_another_businesss_branch(
        self, client_owner_b, item_b, store_a
    ):
        response = client_owner_b.post(
            STOCK, {"item": str(item_b.id), "store": str(store_a.id)}, format="json"
        )
        assert response.status_code == 400

    def test_the_orm_refuses_a_cross_business_read(self, stock_a, tenant_b):
        with transaction.atomic(), tenant_context(tenant_b.id):
            assert StockItem.all_objects.count() == 0
            assert StockMovement.all_objects.count() == 0
