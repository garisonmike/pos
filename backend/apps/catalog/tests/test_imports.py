"""
Bulk item import.

The behaviour that matters most is what happens when a file is *partly* wrong,
because that is every real file. Good rows must go in, bad rows must come back
with the row number and something actionable, and the two phases must not
disagree about which rows are which.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import transaction

from apps.catalog.models import Barcode, Category, Item, TaxRate
from apps.core.tenancy import tenant_context
from apps.inventory.models import StockItem

pytestmark = pytest.mark.django_db

VALIDATE = "/api/v1/items/import/validate/"
COMMIT = "/api/v1/items/import/commit/"
TEMPLATE = "/api/v1/items/import/template/"

HEADER = "sku,name,price,category,tax_rate,cost,barcodes,opening_quantity,reorder_level\n"


def csv_file(body: str, name: str = "items.csv"):
    """Build an uploadable CSV from a string."""
    upload = io.BytesIO((HEADER + body).encode())
    upload.name = name
    return upload


def upload(client, url: str, body: str, **extra):
    return client.post(url, {"file": csv_file(body), **extra}, format="multipart")


class TestValidate:
    def test_a_clean_file_reports_no_errors(self, client_manager_a, tax_rate_a):
        response = upload(
            client_manager_a,
            VALIDATE,
            "SUGAR-1KG,Sugar 1kg,180.00,Dry Goods,VAT 16%,150.00,,40,10\n",
        )
        assert response.status_code == 200

        body = response.json()
        assert (body["total"], body["valid"], body["invalid"]) == (1, 1, 0)
        assert body["token"]

    def test_validating_writes_nothing(self, client_manager_a, tax_rate_a, tenant_a):
        upload(
            client_manager_a,
            VALIDATE,
            "SUGAR-1KG,Sugar 1kg,180.00,Dry Goods,VAT 16%,150.00,,40,10\n",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Item.objects.count() == 0
            assert Category.objects.count() == 0

    def test_errors_name_the_row_the_field_and_the_fix(self, client_manager_a, tax_rate_a):
        body = (
            "SUGAR-1KG,Sugar 1kg,18O.00,,VAT 16%,,,,\n"  # letter O, not zero
            ",Nameless,100.00,,VAT 16%,,,,\n"  # no sku
            "BREAD-400,Bread,100.00,,Nonexistent Rate,,,,\n"
        )
        response = upload(client_manager_a, VALIDATE, body)
        errors = {(e["row"], e["field"]): e["message"] for e in response.json()["errors"]}

        assert "letter O" in errors[(2, "price")]
        assert errors[(3, "sku")] == "Required."
        assert "Unknown tax rate" in errors[(4, "tax_rate")]

    def test_good_and_bad_rows_are_counted_separately(self, client_manager_a, tax_rate_a):
        body = (
            "GOOD-1,Good one,100.00,,VAT 16%,,,,\n"
            "BAD-1,Bad one,not-a-price,,VAT 16%,,,,\n"
            "GOOD-2,Good two,200.00,,VAT 16%,,,,\n"
        )
        response = upload(client_manager_a, VALIDATE, body).json()
        assert (response["total"], response["valid"], response["invalid"]) == (3, 2, 1)

    def test_a_repeated_sku_in_one_file_is_caught(self, client_manager_a, tax_rate_a):
        body = "SAME,First,100.00,,,,,,\nSAME,Second,200.00,,,,,,\n"
        errors = upload(client_manager_a, VALIDATE, body).json()["errors"]
        assert any("Repeated in this file" in e["message"] for e in errors)

    def test_a_barcode_belonging_to_another_item_is_named(
        self, client_manager_a, item_a, tenant_a
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="6161100234567")

        body = "BREAD-400,Bread,100.00,,,,6161100234567,,\n"
        errors = upload(client_manager_a, VALIDATE, body).json()["errors"]
        assert any("already belongs to Sugar 1kg" in e["message"] for e in errors)

    def test_a_missing_required_column_is_a_file_level_error(self, client_manager_a):
        upload_file = io.BytesIO(b"sku,name\nX,Y\n")
        upload_file.name = "bad.csv"
        response = client_manager_a.post(
            VALIDATE, {"file": upload_file}, format="multipart"
        )
        assert response.status_code == 400
        assert "price" in response.json()["detail"]

    def test_a_cashier_cannot_import(self, client_cashier_a, tax_rate_a):
        response = upload(client_cashier_a, VALIDATE, "X,Y,1.00,,,,,,\n")
        assert response.status_code == 403


class TestCommit:
    def _validated(self, client, body: str) -> str:
        return upload(client, VALIDATE, body).json()["token"]

    def test_valid_rows_are_imported(self, client_manager_a, tax_rate_a, tenant_a):
        body = "SUGAR-1KG,Sugar 1kg,180.00,Dry Goods,VAT 16%,150.00,,40,10\n"
        token = self._validated(client_manager_a, body)

        response = upload(client_manager_a, COMMIT, body, token=token)
        assert response.status_code == 200
        assert response.json()["created"] == 1

        with transaction.atomic(), tenant_context(tenant_a.id):
            item = Item.objects.get(sku="SUGAR-1KG")
        assert item.price_cents == 18000
        assert item.cost_cents == 15000

    def test_bad_rows_are_skipped_and_good_ones_still_land(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        """The whole reason for per-row handling.

        Refusing four hundred good rows over three bad ones means an afternoon
        of spreadsheet editing before anything works at all.
        """
        body = (
            "GOOD-1,Good one,100.00,,VAT 16%,,,,\n"
            "BAD-1,Bad one,not-a-price,,VAT 16%,,,,\n"
            "GOOD-2,Good two,200.00,,VAT 16%,,,,\n"
        )
        token = self._validated(client_manager_a, body)
        report = upload(client_manager_a, COMMIT, body, token=token).json()

        assert report["created"] == 2
        assert report["invalid"] == 1

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert set(Item.objects.values_list("sku", flat=True)) == {"GOOD-1", "GOOD-2"}

    def test_reimporting_updates_rather_than_duplicates(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        """Someone correcting a spreadsheet uploads it again."""
        first = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16%,,,,\n"
        upload(client_manager_a, COMMIT, first, token=self._validated(client_manager_a, first))

        second = "SUGAR-1KG,Sugar 1kg,195.00,,VAT 16%,,,,\n"
        report = upload(
            client_manager_a, COMMIT, second, token=self._validated(client_manager_a, second)
        ).json()

        assert report["updated"] == 1
        assert report["created"] == 0

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Item.objects.filter(sku="SUGAR-1KG").count() == 1
            assert Item.objects.get(sku="SUGAR-1KG").price_cents == 19500

    def test_several_barcodes_arrive_from_one_cell(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        body = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16%,,6161100111111;6161100222222,,\n"
        upload(client_manager_a, COMMIT, body, token=self._validated(client_manager_a, body))

        with transaction.atomic(), tenant_context(tenant_a.id):
            codes = set(
                Barcode.objects.filter(item__sku="SUGAR-1KG").values_list("code", flat=True)
            )
        assert codes == {"6161100111111", "6161100222222"}

    def test_unknown_categories_are_created_and_reported(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        body = "SUGAR-1KG,Sugar 1kg,180.00,Dry Goods,VAT 16%,,,,\n"
        report = upload(
            client_manager_a, COMMIT, body, token=self._validated(client_manager_a, body)
        ).json()

        assert report["categories_created"] == ["Dry Goods"]
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Category.objects.filter(name="Dry Goods").exists()

    def test_categories_differing_only_by_case_do_not_double_up(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        body = (
            "A-1,First,100.00,Dry Goods,VAT 16%,,,,\n"
            "A-2,Second,100.00,dry goods,VAT 16%,,,,\n"
        )
        upload(client_manager_a, COMMIT, body, token=self._validated(client_manager_a, body))

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Category.objects.filter(name__iexact="dry goods").count() == 1

    def test_an_unknown_tax_rate_fails_its_row_rather_than_being_created(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        """A typo here would silently mis-tax every sale filed against it."""
        body = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16 %,,,,\n"
        report = upload(
            client_manager_a, COMMIT, body, token=self._validated(client_manager_a, body)
        ).json()

        assert report["invalid"] == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert TaxRate.objects.count() == 1
            assert not Item.objects.exists()

    def test_opening_stock_arrives_through_the_ledger(
        self, client_manager_a, tax_rate_a, store_a, tenant_a
    ):
        body = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16%,,,40,10\n"
        upload(client_manager_a, COMMIT, body, token=self._validated(client_manager_a, body))

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock = StockItem.objects.get(item__sku="SUGAR-1KG")
            assert stock.quantity == Decimal("40.000")
            assert stock.reorder_level == Decimal("10.000")
            assert stock.movements.count() == 1

    def test_a_service_row_imports_without_stock(self, client_manager_a, store_a, tenant_a):
        upload_file = io.BytesIO(
            b"sku,name,price,item_type,track_stock,duration_minutes,is_price_variable\n"
            b"SVC-BRAID,Braiding,500.00,SERVICE,no,120,yes\n"
        )
        upload_file.name = "services.csv"
        validated = client_manager_a.post(
            VALIDATE, {"file": upload_file}, format="multipart"
        ).json()
        assert validated["invalid"] == 0

        upload_file.seek(0)
        client_manager_a.post(
            COMMIT, {"file": upload_file, "token": validated["token"]}, format="multipart"
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            service = Item.objects.get(sku="SVC-BRAID")
        assert service.track_stock is False
        assert service.duration_minutes == 120
        assert service.is_price_variable is True


class TestTheTokenTiesThePhasesTogether:
    def _validated(self, client, body: str) -> str:
        return upload(client, VALIDATE, body).json()["token"]

    def test_commit_without_a_token_is_refused(self, client_manager_a, tax_rate_a):
        response = upload(client_manager_a, COMMIT, "A,B,1.00,,,,,,\n")
        assert response.status_code == 400
        assert response.json()["code"] == "token_required"

    def test_a_token_from_a_different_file_is_refused(self, client_manager_a, tax_rate_a):
        """Otherwise the report someone approved and the rows imported differ."""
        reviewed = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16%,,,,\n"
        token = self._validated(client_manager_a, reviewed)

        swapped = "SUGAR-1KG,Sugar 1kg,1.00,,VAT 16%,,,,\n"
        response = upload(client_manager_a, COMMIT, swapped, token=token)

        assert response.status_code == 400
        assert "not the one that was checked" in response.json()["detail"]

    def test_an_expired_token_is_refused(self, client_manager_a, tax_rate_a):
        body = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16%,,,,\n"
        token = self._validated(client_manager_a, body)

        cache.delete(f"item-import:{token}")  # what expiry does

        response = upload(client_manager_a, COMMIT, body, token=token)
        assert response.status_code == 400
        assert "expired" in response.json()["detail"]

    def test_another_businesss_token_is_refused(
        self, client_manager_a, client_owner_b, tax_rate_a
    ):
        body = "SUGAR-1KG,Sugar 1kg,180.00,,VAT 16%,,,,\n"
        token = self._validated(client_manager_a, body)

        response = upload(client_owner_b, COMMIT, body, token=token)
        assert response.status_code == 400
        assert "does not belong to this business" in response.json()["detail"]


class TestCommitRechecksReferences:
    """References are resolved again at commit, not carried over from validate.

    Time passes between the two calls - someone reads the report, makes a cup of
    tea, and meanwhile a colleague renames a tax rate. A row pointing at it must
    fail like any other bad reference, while the rest of the file still imports.
    """

    def _validated(self, client, body: str) -> str:
        return upload(client, VALIDATE, body).json()["token"]

    def test_a_tax_rate_deleted_after_validation_fails_only_its_own_row(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        body = (
            "TAXED-1,Taxed item,180.00,,VAT 16%,,,,\n"
            "PLAIN-1,Untaxed item,90.00,,,,,,\n"
        )
        token = self._validated(client_manager_a, body)
        assert upload(client_manager_a, VALIDATE, body).json()["invalid"] == 0

        with transaction.atomic(), tenant_context(tenant_a.id):
            TaxRate.objects.filter(pk=tax_rate_a.pk).delete()

        report = upload(client_manager_a, COMMIT, body, token=token).json()

        assert report["invalid"] == 1
        assert report["created"] == 1
        assert any("Unknown tax rate" in e["message"] for e in report["errors"])

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert set(Item.objects.values_list("sku", flat=True)) == {"PLAIN-1"}

    def test_a_tax_rate_renamed_after_validation_fails_its_row(
        self, client_manager_a, tax_rate_a, tenant_a
    ):
        body = "TAXED-1,Taxed item,180.00,,VAT 16%,,,,\n"
        token = self._validated(client_manager_a, body)

        with transaction.atomic(), tenant_context(tenant_a.id):
            tax_rate_a.name = "VAT standard"
            tax_rate_a.save(update_fields=["name"])

        report = upload(client_manager_a, COMMIT, body, token=token).json()

        assert report["invalid"] == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert not Item.objects.exists()

    def test_a_barcode_claimed_after_validation_fails_its_row(
        self, client_manager_a, tax_rate_a, item_a, tenant_a
    ):
        body = "BREAD-400,Bread,100.00,,VAT 16%,,6161100999999,,\n"
        token = self._validated(client_manager_a, body)

        with transaction.atomic(), tenant_context(tenant_a.id):
            Barcode.objects.create(tenant=tenant_a, item=item_a, code="6161100999999")

        report = upload(client_manager_a, COMMIT, body, token=token).json()
        assert report["invalid"] == 1


class TestTemplate:
    def test_the_template_downloads_as_csv(self, client_manager_a):
        response = client_manager_a.get(TEMPLATE)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"

    def test_it_shows_both_a_product_and_a_service(self, client_manager_a):
        """The service row is the one people get stuck on."""
        content = client_manager_a.get(TEMPLATE).content.decode()
        assert "SUGAR-1KG" in content
        assert "SVC-BRAID" in content
        assert "SERVICE" in content


class TestImportIsolation:
    def test_an_import_never_touches_another_business(
        self, client_owner_b, tax_rate_a, item_a, tenant_a, tenant_b
    ):
        """Business B importing an SKU that exists in A must not update A's row."""
        body = "SUGAR-1KG,Their sugar,999.00,,,,,,\n"
        token = upload(client_owner_b, VALIDATE, body).json()["token"]
        upload(client_owner_b, COMMIT, body, token=token)

        with transaction.atomic(), tenant_context(tenant_a.id):
            item_a.refresh_from_db()
        assert item_a.name == "Sugar 1kg"
        assert item_a.price_cents == 18000

        with transaction.atomic(), tenant_context(tenant_b.id):
            theirs = Item.objects.get(sku="SUGAR-1KG")
        assert theirs.name == "Their sugar"

    def test_a_tax_rate_from_another_business_is_not_found(
        self, client_owner_b, tax_rate_a
    ):
        """Names are resolved within the caller's own business only."""
        body = "X-1,Thing,100.00,,VAT 16%,,,,\n"
        errors = upload(client_owner_b, VALIDATE, body).json()["errors"]
        assert any("Unknown tax rate" in e["message"] for e in errors)
