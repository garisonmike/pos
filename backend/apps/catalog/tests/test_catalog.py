"""
The catalogue: categories, tax rates, items and barcodes.

Two themes run through this file. One is that a single ``Item`` model has to
serve a bag of sugar and a haircut without either being second-class. The other
is tax: whether a price includes it is decided per rate, so one business can mix
both, and the arithmetic has to be exact in either direction.
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.catalog.models import Barcode, Category, Item, TaxRate
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db

ITEMS = "/api/v1/items/"
CATEGORIES = "/api/v1/categories/"
TAX_RATES = "/api/v1/tax-rates/"


class TestCategories:
    def test_a_manager_can_create_one(self, client_manager_a):
        response = client_manager_a.post(CATEGORIES, {"name": "Dry Goods"}, format="json")
        assert response.status_code == 201
        assert response.json()["slug"] == "dry-goods"

    def test_a_cashier_cannot(self, client_cashier_a):
        assert (
            client_cashier_a.post(CATEGORIES, {"name": "Dry Goods"}, format="json").status_code
            == 403
        )

    def test_a_cashier_can_read_them(self, client_cashier_a, category_a):
        response = client_cashier_a.get(CATEGORIES)
        assert response.status_code == 200
        assert response.json()["results"][0]["name"] == "Dry Goods"

    def test_a_duplicate_name_is_refused(self, client_manager_a, category_a):
        response = client_manager_a.post(CATEGORIES, {"name": "Dry Goods"}, format="json")
        assert response.status_code == 400

    def test_the_same_name_in_another_business_is_fine(self, client_owner_b, category_a):
        """Two shops may both have a 'Dry Goods'."""
        response = client_owner_b.post(CATEGORIES, {"name": "Dry Goods"}, format="json")
        assert response.status_code == 201

    def test_subcategories_work(self, client_manager_a, category_a):
        response = client_manager_a.post(
            CATEGORIES, {"name": "Sugar", "parent": str(category_a.id)}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["parent_name"] == "Dry Goods"

    def test_a_category_cannot_be_its_own_parent(self, client_manager_a, category_a):
        response = client_manager_a.patch(
            f"{CATEGORIES}{category_a.id}/", {"parent": str(category_a.id)}, format="json"
        )
        assert response.status_code == 400

    def test_another_businesss_category_cannot_be_a_parent(
        self, client_manager_a, tenant_b
    ):
        with transaction.atomic(), tenant_context(tenant_b.id):
            theirs = Category.objects.create(tenant=tenant_b, name="Theirs", slug="theirs")

        response = client_manager_a.post(
            CATEGORIES, {"name": "Mine", "parent": str(theirs.id)}, format="json"
        )
        assert response.status_code == 400

    def test_a_category_with_items_cannot_be_deleted(
        self, client_manager_a, category_a, item_a, tenant_a
    ):
        """Deleting it would change what a year of reports says."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            item_a.category = category_a
            item_a.save(update_fields=["category"])

        response = client_manager_a.delete(f"{CATEGORIES}{category_a.id}/")
        assert response.status_code == 400
        assert "still has items" in response.json()["detail"]


class TestTaxRates:
    def test_a_rate_is_created_in_basis_points(self, client_manager_a):
        response = client_manager_a.post(
            TAX_RATES, {"name": "VAT 16%", "rate_bps": 1600, "is_inclusive": True}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["rate_percent"] == 16.0

    def test_a_rate_over_one_hundred_percent_is_refused(self, client_manager_a):
        """Always a unit mistake, and it would mis-price everything attached."""
        response = client_manager_a.post(
            TAX_RATES, {"name": "Wrong", "rate_bps": 160000}, format="json"
        )
        assert response.status_code == 400
        assert "basis points" in str(response.json())

    def test_marking_a_new_default_stands_the_old_one_down(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        """A unique constraint forbids two; this makes the swap just work."""
        response = client_manager_a.post(
            TAX_RATES,
            {"name": "Zero rated", "rate_bps": 0, "is_default": True},
            format="json",
        )
        assert response.status_code == 201

        with transaction.atomic(), tenant_context(tenant_a.id):
            defaults = list(
                TaxRate.objects.filter(tenant=tenant_a, is_default=True).values_list(
                    "name", flat=True
                )
            )
        assert defaults == ["Zero rated"]

    def test_a_rate_in_use_cannot_be_deleted(self, client_manager_a, item_a, tax_rate_a):
        """Past sales recorded VAT at the rate in force then."""
        response = client_manager_a.delete(f"{TAX_RATES}{tax_rate_a.id}/")
        assert response.status_code == 400
        assert "Deactivate it instead" in response.json()["detail"]


class TestTaxBreakdown:
    """The arithmetic, per item, in both directions.

    ``is_inclusive`` lives on the rate rather than the business, so these two
    items sit in one catalogue and total differently. That is the case a
    per-business setting could not express.
    """

    def test_an_inclusive_price_is_split_out_of_the_marked_price(
        self, client_cashier_a, item_a
    ):
        response = client_cashier_a.get(f"{ITEMS}{item_a.id}/")
        breakdown = response.json()["tax_breakdown"]

        assert breakdown["gross_cents"] == 18000  # the customer pays exactly this
        assert breakdown["net_cents"] == 15517
        assert breakdown["tax_cents"] == 2483
        assert breakdown["is_inclusive"] is True

    def test_net_plus_tax_equals_what_the_customer_pays(self, client_cashier_a, item_a):
        breakdown = client_cashier_a.get(f"{ITEMS}{item_a.id}/").json()["tax_breakdown"]
        assert breakdown["net_cents"] + breakdown["tax_cents"] == breakdown["gross_cents"]

    def test_an_exclusive_price_has_tax_added_on_top(
        self, client_manager_a, exclusive_rate_a, tenant_a
    ):
        response = client_manager_a.post(
            ITEMS,
            {
                "sku": "TRADE-1",
                "name": "Trade sugar",
                "price_cents": 15517,
                "tax_rate": str(exclusive_rate_a.id),
            },
            format="json",
        )
        assert response.status_code == 201

        breakdown = response.json()["tax_breakdown"]
        assert breakdown["net_cents"] == 15517
        assert breakdown["tax_cents"] == 2483
        assert breakdown["gross_cents"] == 18000
        assert breakdown["is_inclusive"] is False

    def test_one_business_can_hold_both_kinds_at_once(
        self, client_manager_a, item_a, exclusive_rate_a
    ):
        """The point of the per-rate flag, stated as a single assertion."""
        client_manager_a.post(
            ITEMS,
            {
                "sku": "TRADE-1",
                "name": "Trade sugar",
                "price_cents": 15517,
                "tax_rate": str(exclusive_rate_a.id),
            },
            format="json",
        )
        rows = {row["sku"]: row for row in client_manager_a.get(ITEMS).json()["results"]}

        assert rows["SUGAR-1KG"]["tax_breakdown"]["is_inclusive"] is True
        assert rows["TRADE-1"]["tax_breakdown"]["is_inclusive"] is False
        # Different bases, same amount actually charged.
        assert rows["SUGAR-1KG"]["tax_breakdown"]["gross_cents"] == 18000
        assert rows["TRADE-1"]["tax_breakdown"]["gross_cents"] == 18000

    def test_an_untaxed_item_is_all_net(self, client_manager_a):
        response = client_manager_a.post(
            ITEMS, {"sku": "EXEMPT-1", "name": "Exempt", "price_cents": 10000}, format="json"
        )
        breakdown = response.json()["tax_breakdown"]
        assert (breakdown["net_cents"], breakdown["tax_cents"]) == (10000, 0)

    def test_a_zero_rated_item_carries_no_tax(self, client_manager_a, tenant_a):
        """Unprocessed foods are zero-rated, and are much of a duka's shelf."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            zero = TaxRate.objects.create(
                tenant=tenant_a, name="Zero rated", rate_bps=0, is_inclusive=True
            )

        response = client_manager_a.post(
            ITEMS,
            {"sku": "MAIZE-2KG", "name": "Maize flour", "price_cents": 21000,
             "tax_rate": str(zero.id)},
            format="json",
        )
        breakdown = response.json()["tax_breakdown"]
        assert (breakdown["net_cents"], breakdown["tax_cents"]) == (21000, 0)


class TestItemsAndServices:
    """One model, two kinds of thing, neither second-class."""

    def test_a_product_is_created_with_its_barcodes(self, client_manager_a):
        response = client_manager_a.post(
            ITEMS,
            {
                "sku": "BREAD-400",
                "name": "Bread 400g",
                "price_cents": 6500,
                "barcodes": ["6161100111111", "6161100222222"],
            },
            format="json",
        )
        assert response.status_code == 201

        codes = [b["code"] for b in response.json()["barcodes"]]
        assert codes == ["6161100111111", "6161100222222"]
        assert response.json()["barcodes"][0]["is_primary"] is True

    def test_a_service_is_created_without_stock(self, client_manager_a):
        response = client_manager_a.post(
            ITEMS,
            {
                "sku": "SVC-CUT",
                "name": "Haircut",
                "price_cents": 30000,
                "item_type": "SERVICE",
                "track_stock": False,
                "duration_minutes": 45,
                "is_price_variable": True,
            },
            format="json",
        )
        assert response.status_code == 201

        body = response.json()
        assert body["duration_minutes"] == 45
        assert body["is_price_variable"] is True
        assert body["stock"] == []

    def test_a_service_cannot_track_stock(self, client_manager_a):
        """A haircut has no shelf. Always a mistake, never a preference."""
        response = client_manager_a.post(
            ITEMS,
            {
                "sku": "SVC-CUT",
                "name": "Haircut",
                "price_cents": 30000,
                "item_type": "SERVICE",
                "track_stock": True,
            },
            format="json",
        )
        assert response.status_code == 400
        assert "no shelf to count" in str(response.json())

    def test_untracked_items_report_no_stock_rather_than_zero(
        self, client_cashier_a, service_a
    ):
        """'Not tracked' and 'none left' are different, and the till must tell them apart."""
        response = client_cashier_a.get(f"{ITEMS}{service_a.id}/")
        assert response.json()["stock"] == []

    def test_a_product_cannot_be_priced_by_the_hour(self, client_manager_a):
        response = client_manager_a.post(
            ITEMS,
            {"sku": "ODD-1", "name": "Odd", "price_cents": 100, "unit": "HOUR"},
            format="json",
        )
        assert response.status_code == 400

    def test_a_duplicate_sku_is_refused(self, client_manager_a, item_a):
        response = client_manager_a.post(
            ITEMS, {"sku": "SUGAR-1KG", "name": "Other sugar", "price_cents": 100}, format="json"
        )
        assert response.status_code == 400

    def test_the_same_sku_in_another_business_is_fine(self, client_owner_b, item_a):
        response = client_owner_b.post(
            ITEMS, {"sku": "SUGAR-1KG", "name": "Their sugar", "price_cents": 100}, format="json"
        )
        assert response.status_code == 201

    def test_the_till_label_falls_back_to_a_trimmed_name(self, client_manager_a):
        response = client_manager_a.post(
            ITEMS,
            {
                "sku": "LONG-1",
                "name": "Extremely Long Product Name That Will Not Fit On A Button",
                "price_cents": 100,
            },
            format="json",
        )
        assert len(response.json()["till_label"]) == 24

    def test_availability_is_separate_from_being_delisted(self, client_manager_a, item_a):
        """'Out of season' must not require deleting and re-creating an item."""
        response = client_manager_a.patch(
            f"{ITEMS}{item_a.id}/", {"is_available": False}, format="json"
        )
        assert response.status_code == 200

        body = response.json()
        assert body["is_active"] is True
        assert body["is_available"] is False
        assert body["is_sellable"] is False

    def test_deleting_an_item_deactivates_it(self, client_manager_a, item_a, tenant_a):
        assert client_manager_a.delete(f"{ITEMS}{item_a.id}/").status_code == 204

        with transaction.atomic(), tenant_context(tenant_a.id):
            item_a.refresh_from_db()
        assert item_a.is_active is False

    def test_a_price_change_is_audited(self, client_manager_a, item_a, tenant_a):
        """The most sensitive edit in the catalogue."""
        client_manager_a.patch(f"{ITEMS}{item_a.id}/", {"price_cents": 20000}, format="json")

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(
                action=AuditAction.UPDATE, entity_type="catalog.Item"
            ).first()

        assert entry.before["price_cents"] == 18000
        assert entry.after["price_cents"] == 20000

    def test_a_cashier_cannot_change_a_price(self, client_cashier_a, item_a):
        response = client_cashier_a.patch(
            f"{ITEMS}{item_a.id}/", {"price_cents": 1}, format="json"
        )
        assert response.status_code == 403


class TestBarcodes:
    def test_any_barcode_on_an_item_resolves_to_it(self, client_cashier_a, item_a, tenant_a):
        """A case pack and a single unit are the same product with two codes."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="AAA", is_primary=True)
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="BBB")

        for code in ("AAA", "BBB"):
            response = client_cashier_a.get(f"{ITEMS}lookup/?barcode={code}")
            assert response.status_code == 200
            assert response.json()["sku"] == "SUGAR-1KG"

    def test_an_unknown_barcode_returns_404(self, client_cashier_a, item_a):
        response = client_cashier_a.get(f"{ITEMS}lookup/?barcode=NOPE")
        assert response.status_code == 404
        assert response.json()["code"] == "barcode_not_found"

    def test_another_businesss_barcode_does_not_resolve(
        self, client_owner_b, item_a, tenant_a
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="AAA")

        assert client_owner_b.get(f"{ITEMS}lookup/?barcode=AAA").status_code == 404

    def test_the_same_barcode_in_two_businesses_is_allowed(
        self, client_owner_b, item_a, item_b, tenant_a
    ):
        """Two shops printing their own labels will collide, and neither is wrong."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="SHARED")

        response = client_owner_b.post(
            f"{ITEMS}{item_b.id}/barcodes/", {"code": "SHARED"}, format="json"
        )
        assert response.status_code == 201

    def test_a_barcode_already_used_in_this_business_is_refused_by_name(
        self, client_manager_a, item_a, tenant_a
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="TAKEN")

        response = client_manager_a.post(
            ITEMS,
            {"sku": "OTHER", "name": "Other", "price_cents": 100, "barcodes": ["TAKEN"]},
            format="json",
        )
        assert response.status_code == 400
        assert "Sugar 1kg" in str(response.json())

    def test_a_barcode_can_be_removed(self, client_manager_a, item_a, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            barcode = Barcode.objects.create(tenant=tenant_a, item=item_a, code="GONE")

        response = client_manager_a.delete(f"{ITEMS}{item_a.id}/barcodes/{barcode.id}/")
        assert response.status_code == 204


class TestSearch:
    def test_search_matches_name_sku_and_barcode(self, client_cashier_a, item_a, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="6161100234567")

        for query in ("sugar", "SUGAR-1KG", "61611002"):
            response = client_cashier_a.get(f"{ITEMS}search/?q={query}")
            assert response.status_code == 200, query
            assert [row["sku"] for row in response.json()] == ["SUGAR-1KG"], query

    def test_a_one_character_query_is_refused(self, client_cashier_a):
        assert client_cashier_a.get(f"{ITEMS}search/?q=s").status_code == 400

    def test_search_never_crosses_businesses(self, client_owner_b, item_a):
        assert client_owner_b.get(f"{ITEMS}search/?q=sugar").json() == []


class TestCatalogIsolation:
    """The same bar as milestone 1, applied to every new table."""

    def test_items_are_not_visible_across_businesses(self, client_owner_b, item_a):
        skus = {row["sku"] for row in client_owner_b.get(ITEMS).json()["results"]}
        assert "SUGAR-1KG" not in skus

    def test_another_businesss_item_cannot_be_read(self, client_owner_b, item_a):
        assert client_owner_b.get(f"{ITEMS}{item_a.id}/").status_code == 404

    def test_another_businesss_item_cannot_be_edited(self, client_owner_b, item_a, tenant_a):
        response = client_owner_b.patch(
            f"{ITEMS}{item_a.id}/", {"price_cents": 1}, format="json"
        )
        assert response.status_code == 404

        with transaction.atomic(), tenant_context(tenant_a.id):
            item_a.refresh_from_db()
        assert item_a.price_cents == 18000

    def test_another_businesss_category_cannot_be_read(self, client_owner_b, category_a):
        assert client_owner_b.get(f"{CATEGORIES}{category_a.id}/").status_code == 404

    def test_another_businesss_tax_rate_cannot_be_read(self, client_owner_b, tax_rate_a):
        assert client_owner_b.get(f"{TAX_RATES}{tax_rate_a.id}/").status_code == 404

    def test_an_item_cannot_be_pointed_at_another_businesss_category(
        self, client_owner_b, category_a
    ):
        response = client_owner_b.post(
            ITEMS,
            {
                "sku": "X",
                "name": "X",
                "price_cents": 100,
                "category": str(category_a.id),
            },
            format="json",
        )
        assert response.status_code == 400

    def test_an_item_cannot_be_pointed_at_another_businesss_tax_rate(
        self, client_owner_b, tax_rate_a
    ):
        response = client_owner_b.post(
            ITEMS,
            {"sku": "X", "name": "X", "price_cents": 100, "tax_rate": str(tax_rate_a.id)},
            format="json",
        )
        assert response.status_code == 400

    def test_the_orm_refuses_a_cross_business_read(self, item_a, tenant_b):
        """Below the API, where a forgotten filter would live."""
        with transaction.atomic(), tenant_context(tenant_b.id):
            assert Item.all_objects.filter(sku="SUGAR-1KG").count() == 0
            assert Barcode.all_objects.count() == 0
            assert Category.all_objects.count() == 0
