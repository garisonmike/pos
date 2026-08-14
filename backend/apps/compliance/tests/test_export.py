"""
What a filer actually types in.

The export is the manual adapter's entire output, so the tests are about
whether somebody could sit down with it and fill in a return.
"""

from __future__ import annotations

import csv
import io

import pytest

from apps.compliance.export import documents_for_export, render_csv, render_pdf, rows_for
from apps.compliance.models import ComplianceDocument, ComplianceMode
from apps.compliance.services import issue_credit_note
from apps.core.tenancy import tenant_context

CHECKOUT = "/api/v1/sales/checkout/cash/"


@pytest.fixture
def registered_tenant(tenant_a):
    with tenant_context(tenant_a.id):
        tenant_a.compliance_mode = ComplianceMode.MANUAL
        tenant_a.kra_pin = "P051234567X"
        tenant_a.save()
        return tenant_a


def sell(client, item, **extra) -> dict:
    body = {
        "lines": [{"item_id": str(item.id), "quantity": "1"}],
        "tendered_cents": 18000,
    }
    body.update(extra)
    return client.post(CHECKOUT, body, format="json").json()


def parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


@pytest.mark.django_db
class TestTheCsv:
    def test_it_carries_a_header_row(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            rows = parse(render_csv(documents_for_export(tenant=registered_tenant)))

        assert rows[0][0] == "Document"
        assert "Buyer PIN" in rows[0]

    def test_one_row_per_rate(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """A return is filled in per rate. A single total would make the filer
        do the splitting by hand."""
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            rows = parse(render_csv(documents_for_export(tenant=registered_tenant)))

        assert len(rows) == 2
        assert rows[1][5] == "16%"

    def test_figures_are_written_as_money_not_cents(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """Nobody types cents into a tax form."""
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            rows = parse(render_csv(documents_for_export(tenant=registered_tenant)))

        assert rows[1][6] == "155.17"
        assert rows[1][7] == "24.83"
        assert rows[1][8] == "180.00"

    def test_a_zero_rate_is_named_rather_than_shown_as_a_percentage(
        self, client_cashier_a, cashier_a, registered_tenant, tenant_a, store_a, stock_a
    ):
        from apps.catalog.models import Item
        from apps.compliance.services import issue_invoice
        from apps.sales.services import LineRequest, create_sale, take_cash

        with tenant_context(tenant_a.id):
            bread = Item.objects.create(
                tenant=tenant_a, sku="BREAD", name="Bread", price_cents=6000
            )
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(bread.id), quantity=1)],
            )
            take_cash(sale=sale, tendered_cents=6000, user=cashier_a)
            sale.refresh_from_db()
            issue_invoice(sale=sale)

            rows = parse(render_csv(documents_for_export(tenant=tenant_a)))

        assert rows[1][5] == "Zero-rated"

    def test_a_credit_note_is_written_negative(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """On the form it subtracts. In the database it is stored positive so
        nobody subtracts it twice."""
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            invoice = ComplianceDocument.objects.get(sale_id=settled["id"])
            issue_credit_note(original=invoice, reason="Goods returned")
            rows = parse(render_csv(documents_for_export(tenant=registered_tenant)))

        credit = [row for row in rows if row[1] == "Credit note"][0]
        assert credit[8] == "-180.00"
        assert credit[6].startswith("-")

    def test_both_parties_appear(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            rows = parse(render_csv(documents_for_export(tenant=registered_tenant)))

        assert rows[1][3] == "P051234567X"
        assert rows[1][4] == "P012345678Z"

    def test_windows_line_endings(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """Opened in Excel on Windows more often than anywhere else, and bare
        newlines put the whole file on one row."""
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            text = render_csv(documents_for_export(tenant=registered_tenant))

        assert "\r\n" in text

    def test_an_empty_period_still_produces_a_header(
        self, registered_tenant, tenant_a
    ):
        """A filer who exported the wrong month should see an empty form, not
        an empty file that looks like a failure."""
        with tenant_context(tenant_a.id):
            rows = parse(render_csv(documents_for_export(tenant=registered_tenant)))

        assert rows == [
            [
                "Document",
                "Type",
                "Date",
                "Seller PIN",
                "Buyer PIN",
                "Rate",
                "Net",
                "Tax",
                "Gross",
            ]
        ]


@pytest.mark.django_db
class TestOrdering:
    def test_documents_come_back_in_number_order(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """Reading the gapless series in order is how a filer sees that nothing
        is missing."""
        for _ in range(3):
            sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            numbers = [
                document.invoice_number
                for document in documents_for_export(tenant=registered_tenant)
            ]

        assert numbers == [1, 2, 3]

    def test_a_period_can_be_narrowed(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        from django.utils import timezone

        sell(client_cashier_a, item_a)
        cutoff = timezone.now()
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            after = list(documents_for_export(tenant=registered_tenant, since=cutoff))
            before = list(documents_for_export(tenant=registered_tenant, until=cutoff))

        assert len(after) == 1
        assert len(before) == 1
        assert after[0].invoice_number == 2


@pytest.mark.django_db
class TestThePdf:
    def test_it_produces_a_pdf(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            data = render_pdf(
                documents_for_export(tenant=registered_tenant),
                business_name="Mama Njeri Duka",
            )

        assert data.startswith(b"%PDF")
        assert len(data) > 1000

    def test_it_shows_the_same_rows_as_the_csv(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """One function builds the rows, so the two renderings cannot disagree
        about what was declared."""
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            documents = list(documents_for_export(tenant=registered_tenant))
            rows = rows_for(documents)
            csv_rows = parse(render_csv(documents))[1:]

        assert rows == csv_rows


@pytest.mark.django_db
class TestExportsStayInsideOneBusiness:
    def test_another_businesss_documents_are_not_exported(
        self, client_cashier_a, cashier_a, registered_tenant, tenant_b, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_b.id):
            rows = parse(render_csv(documents_for_export(tenant=tenant_b)))

        assert len(rows) == 1  # header only
