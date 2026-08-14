"""
Receipts.

What a customer is handed. The tests care about two things: that the figures on
the paper reconcile, and that a receipt reprinted later still shows what was
actually charged rather than today's prices.
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.core.tenancy import tenant_context
from apps.sales.receipt_render import render_pdf, render_text

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/sales/checkout/cash/"


@pytest.fixture
def branded_tenant(tenant_a):
    with transaction.atomic(), tenant_context(tenant_a.id):
        tenant_a.receipt_header = "Wholesale & Retail"
        tenant_a.receipt_footer = "Karibu tena"
        tenant_a.kra_pin = "P051234567X"
        tenant_a.phone = "0722000111"
        tenant_a.save()
        return tenant_a


@pytest.fixture
def paid_sale(client_cashier_a, item_a, stock_a, branded_tenant, tenant_a):
    response = client_cashier_a.post(
        CHECKOUT,
        {
            "lines": [{"item_id": str(item_a.id), "quantity": "2"}],
            "tendered_cents": 40000,
        },
        format="json",
    )
    assert response.status_code == 201, response.content

    from apps.sales.models import Sale

    with transaction.atomic(), tenant_context(tenant_a.id):
        return Sale.objects.get(pk=response.json()["id"])


class TestBranding:
    def test_the_business_name_and_branding_appear(self, paid_sale, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            text = render_text(paid_sale)

        assert "MAMA NJERI DUKA" in text
        assert "Wholesale & Retail" in text
        assert "Karibu tena" in text
        assert "P051234567X" in text
        assert "0722000111" in text

    def test_a_business_with_no_branding_still_renders(
        self, client_cashier_a, item_a, stock_a, tenant_a
    ):
        """Branding is optional; a receipt is not."""
        from apps.sales.models import Sale

        response = client_cashier_a.post(
            CHECKOUT,
            {"lines": [{"item_id": str(item_a.id), "quantity": "1"}], "tendered_cents": 20000},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=response.json()["id"])
            text = render_text(sale)

        assert "MAMA NJERI DUKA" in text
        assert "TOTAL" in text


class TestTheFiguresReconcile:
    def test_the_lines_add_up_to_the_total(self, paid_sale, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            text = render_text(paid_sale)

        assert "Sugar 1kg" in text
        assert "2 x 180.00" in text
        assert "360.00" in text

    def test_tax_is_broken_out_by_rate(self, paid_sale, tenant_a):
        """A basket mixing rates is the norm, since unprocessed foods are
        zero-rated and much else is not."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            text = render_text(paid_sale)

        assert "VAT 16%" in text

    def test_the_tender_and_change_are_shown(self, paid_sale, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            text = render_text(paid_sale)

        assert "Tendered" in text
        assert "Change" in text
        assert "40.00" in text  # KES 400 tendered on a KES 360 sale

    def test_the_receipt_number_is_printed(self, paid_sale, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            text = render_text(paid_sale)

        assert paid_sale.receipt_code in text

    def test_rounding_is_shown_rather_than_hidden(
        self, client_manager_a, tenant_a, store_a, tax_rate_a, branded_tenant
    ):
        """A customer handed a figure different from the total is owed the
        difference in writing."""
        from apps.catalog.models import Item
        from apps.sales.models import Sale

        with transaction.atomic(), tenant_context(tenant_a.id):
            odd = Item.objects.create(
                tenant=tenant_a,
                sku="ODD-R",
                name="Odd priced",
                price_cents=18749,
                tax_rate=tax_rate_a,
                track_stock=False,
            )

        response = client_manager_a.post(
            CHECKOUT,
            {"lines": [{"item_id": str(odd.id), "quantity": "1"}], "tendered_cents": 20000},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=response.json()["id"])
            text = render_text(sale)

        assert "Rounding" in text
        assert "TO PAY" in text


class TestSnapshotting:
    def test_a_later_price_change_does_not_alter_the_receipt(
        self, paid_sale, item_a, tenant_a
    ):
        """The whole reason a sale line snapshots its price."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            item_a.price_cents = 99900
            item_a.name = "Sugar 1kg (new packaging)"
            item_a.save()

            text = render_text(paid_sale)

        assert "Sugar 1kg" in text
        assert "999.00" not in text
        assert "180.00" in text


class TestThermalWidth:
    def test_every_line_fits_a_58mm_roll(self, paid_sale, tenant_a):
        """Anything wider wraps on the printer and the columns stop lining up."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            text = render_text(paid_sale)

        for line in text.split("\n"):
            assert len(line) <= 32, f"too wide: {line!r}"

    def test_a_long_item_name_is_trimmed_not_wrapped(
        self, client_cashier_a, tenant_a, store_a, tax_rate_a
    ):
        from apps.catalog.models import Item
        from apps.sales.models import Sale

        with transaction.atomic(), tenant_context(tenant_a.id):
            long_name = Item.objects.create(
                tenant=tenant_a,
                sku="LONG-1",
                name="Extremely Long Product Name That Would Wrap On A Till Roll",
                price_cents=10000,
                tax_rate=tax_rate_a,
                track_stock=False,
            )

        response = client_cashier_a.post(
            CHECKOUT,
            {"lines": [{"item_id": str(long_name.id), "quantity": "1"}], "tendered_cents": 20000},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=response.json()["id"])
            text = render_text(sale)

        for line in text.split("\n"):
            assert len(line) <= 32


class TestPdf:
    def test_a_pdf_is_produced(self, paid_sale, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            pdf = render_pdf(paid_sale)

        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 500

    def test_the_endpoint_serves_text_by_default(self, client_cashier_a, paid_sale):
        response = client_cashier_a.get(f"/api/v1/sales/{paid_sale.id}/receipt/")

        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")
        assert b"MAMA NJERI DUKA" in response.content

    def test_the_endpoint_serves_a_pdf_on_request(self, client_cashier_a, paid_sale):
        """A separate route, not a ?format= parameter.

        DRF reserves that name for content negotiation, so overloading it makes
        the endpoint 404 for a reason nobody would guess from the URL.
        """
        response = client_cashier_a.get(f"/api/v1/sales/{paid_sale.id}/receipt/pdf/")

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")
        assert paid_sale.receipt_code in response["Content-Disposition"]


class TestReceiptIsolation:
    def test_another_businesss_receipt_cannot_be_fetched(self, client_owner_b, paid_sale):
        response = client_owner_b.get(f"/api/v1/sales/{paid_sale.id}/receipt/")
        assert response.status_code == 404
