"""
The reporting endpoints, and the usage summary the operator invoices from.

The platform summary is the one place a reporting bug becomes a billing bug, so
it gets the treatment money gets.
"""

from __future__ import annotations

import csv
import io

import pytest

from apps.core.tenancy import tenant_context
from apps.reports.periods import period_for
from apps.reports.platform import platform_totals, usage_summary
from apps.tenants.models import TenantStatus

CHECKOUT = "/api/v1/sales/checkout/cash/"
SALES = "/api/v1/reports/sales/"
BEST = "/api/v1/reports/best-sellers/"
CASHIERS = "/api/v1/reports/cashiers/"
REFUNDS = "/api/v1/reports/refunds/"
TRADING = "/api/v1/platform/trading/"


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
class TestWhoMayReadAReport:
    @pytest.mark.parametrize("url", [SALES, BEST, CASHIERS, REFUNDS])
    def test_a_manager_may(self, client_manager_a, store_a, url):
        assert client_manager_a.get(url).status_code == 200

    @pytest.mark.parametrize("url", [SALES, BEST, CASHIERS, REFUNDS])
    def test_a_cashier_may_not(self, client_cashier_a, store_a, url):
        """A cashier's job is the counter. What the business took last month is
        not theirs to read, and cashier performance least of all."""
        assert client_cashier_a.get(url).status_code == 403

    def test_it_needs_authentication(self, anon_client):
        assert anon_client.get(SALES).status_code == 401


@pytest.mark.django_db
class TestTheSalesEndpoint:
    def test_it_returns_the_period_and_its_figures(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        body = client_manager_a.get(SALES).json()

        assert body["granularity"] == "day"
        assert body["periods"][0]["sale_count"] == 1
        assert body["periods"][0]["gross_cents"] == 18000

    def test_cash_and_total_are_both_present(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        taken = client_manager_a.get(SALES).json()["periods"][0]["taken"]

        assert taken["cash_cents"] == 18000
        assert taken["total_cents"] == 18000

    def test_a_range_returns_one_row_per_bucket(
        self, client_manager_a, store_a
    ):
        body = client_manager_a.get(
            SALES, {"since": "2026-08-01", "until": "2026-08-03"}
        ).json()

        assert len(body["periods"]) == 3

    def test_a_bad_granularity_is_refused(self, client_manager_a, store_a):
        response = client_manager_a.get(SALES, {"granularity": "fortnight"})

        assert response.status_code == 400
        assert response.json()["code"] == "bad_granularity"

    def test_a_bad_date_is_refused_rather_than_defaulted(
        self, client_manager_a, store_a
    ):
        """Falling back to today would answer a different question than the one
        asked, and look like it worked."""
        response = client_manager_a.get(SALES, {"on": "last Tuesday"})

        assert response.status_code == 400
        assert response.json()["code"] == "bad_on"

    def test_an_absurd_range_is_refused(self, client_manager_a, store_a):
        response = client_manager_a.get(
            SALES, {"since": "2000-01-01", "until": "2026-08-15"}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "bad_period"


@pytest.mark.django_db
class TestDownloads:
    def test_the_csv_carries_the_same_figures(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        response = client_manager_a.get(SALES + "csv/")
        rows = list(csv.reader(io.StringIO(response.content.decode())))

        assert response["Content-Type"] == "text/csv"
        assert rows[0][0] == "Period"
        assert rows[1][2] == "180.00"

    def test_the_pdf_is_a_pdf(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        response = client_manager_a.get(SALES + "pdf/")

        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_downloads_are_attachments(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        response = client_manager_a.get(SALES + "csv/")

        assert "attachment" in response["Content-Disposition"]

    def test_a_format_parameter_is_not_how_this_works(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        """DRF reserves ``format`` and answers 404 on a value it does not
        recognise. Every content type here has its own path - the third place
        in this codebase that rule applies."""
        sell(client_cashier_a, item_a)

        assert client_manager_a.get(SALES, {"format": "pdf"}).status_code == 404
        assert client_manager_a.get(SALES + "pdf/").status_code == 200

    def test_the_cashier_csv_prints_denominators_beside_rates(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        rows = list(
            csv.reader(io.StringIO(client_manager_a.get(CASHIERS + "csv/").content.decode()))
        )

        assert "Sales" in rows[0]
        assert "Discounted sales" in rows[0]
        assert "Discount rate" in rows[0]


@pytest.mark.django_db
class TestTheCashierEndpointSaysHowToReadIt:
    def test_it_carries_the_framing_with_the_figures(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        """The framing is a design decision, not presentation - so it travels
        with the data rather than living only in a screen somebody might not
        build the same way."""
        sell(client_cashier_a, item_a)

        body = client_manager_a.get(CASHIERS).json()

        assert "quiet shift" in body["note"]
        assert "who authorised" in body["note"]


@pytest.mark.django_db
class TestBestSellersEndpoint:
    def test_it_can_be_ordered_either_way(
        self, client_manager_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        by_revenue = client_manager_a.get(BEST).json()
        by_quantity = client_manager_a.get(BEST, {"order": "quantity"}).json()

        assert by_revenue["order"] == "revenue"
        assert by_quantity["order"] == "quantity"

    def test_a_bad_order_is_refused(self, client_manager_a, store_a):
        response = client_manager_a.get(BEST, {"order": "alphabetical"})

        assert response.status_code == 400
        assert response.json()["code"] == "bad_order"


@pytest.mark.django_db
class TestReportsStayInsideOneBusiness:
    def test_another_businesss_sales_are_not_counted(
        self, client_manager_a, client_cashier_a, client_owner_b, item_a, stock_a, store_b
    ):
        sell(client_cashier_a, item_a)

        theirs = client_owner_b.get(SALES).json()

        assert theirs["periods"][0]["sale_count"] == 0
        assert theirs["periods"][0]["gross_cents"] == 0

    def test_another_businesss_items_are_not_ranked(
        self, client_cashier_a, client_owner_b, item_a, stock_a, store_b
    ):
        sell(client_cashier_a, item_a)

        assert client_owner_b.get(BEST).json()["items"] == []

    def test_another_businesss_staff_do_not_appear(
        self, client_cashier_a, client_owner_b, item_a, stock_a, store_b
    ):
        sell(client_cashier_a, item_a)

        assert client_owner_b.get(CASHIERS).json()["cashiers"] == []


@pytest.mark.django_db
class TestThePlatformUsageSummary:
    def test_only_a_platform_admin_may_read_it(
        self, client_owner_a, client_manager_a, anon_client
    ):
        assert client_owner_a.get(TRADING).status_code == 403
        assert client_manager_a.get(TRADING).status_code == 403
        assert anon_client.get(TRADING).status_code == 401

    def test_it_reads_across_businesses(
        self, client_platform, client_cashier_a, client_owner_b, item_a, stock_a,
        item_b, stock_b, store_b,
    ):
        sell(client_cashier_a, item_a)
        sell(client_owner_b, item_b, tendered=25000)

        body = client_platform.get(TRADING).json()
        by_slug = {row["slug"]: row for row in body["tenants"]}

        assert by_slug["mama-njeri"]["gross_cents"] == 18000
        assert by_slug["kwa-baba"]["gross_cents"] == 25000

    def test_a_suspended_business_still_appears(
        self, tenant_a, client_cashier_a, item_a, stock_a
    ):
        """A business suspended halfway through a month still traded for the
        part before, and still owes for it. Dropping it would quietly forgive
        an invoice with nobody able to notice which."""
        from apps.core.tenancy import bypass_rls

        sell(client_cashier_a, item_a)

        with bypass_rls():
            tenant_a.status = TenantStatus.SUSPENDED
            tenant_a.save()

        rows = usage_summary(period_for(tenant_a))
        by_slug = {row.slug: row for row in rows}

        assert by_slug["mama-njeri"].status == TenantStatus.SUSPENDED
        assert by_slug["mama-njeri"].gross_cents == 18000

    def test_a_business_that_traded_nothing_appears_at_zero(
        self, tenant_a, tenant_b, store_a, store_b
    ):
        """An absence and a zero are different facts. An invoicing run needs to
        tell 'did not trade' from 'is not in the list'."""
        rows = usage_summary(period_for(tenant_a))

        assert len(rows) >= 2
        assert all(row.sale_count == 0 for row in rows)

    def test_totals_count_trading_businesses_separately(
        self, tenant_a, tenant_b, client_cashier_a, item_a, stock_a, store_b
    ):
        sell(client_cashier_a, item_a)

        rows = usage_summary(period_for(tenant_a))
        totals = platform_totals(rows)

        assert totals.tenant_count >= 2
        assert totals.trading_tenant_count == 1
        assert totals.gross_cents == 18000

    def test_the_cash_and_mpesa_split_is_kept(
        self, tenant_a, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        row = {r.slug: r for r in usage_summary(period_for(tenant_a))}["mama-njeri"]

        assert row.cash_cents == 18000
        assert row.mpesa_cents == 0

    def test_active_devices_and_users_are_counted(
        self, tenant_a, device_a, cashier_a, owner_a
    ):
        rows = {row.slug: row for row in usage_summary(period_for(tenant_a))}

        assert rows["mama-njeri"].active_device_count == 1
        assert rows["mama-njeri"].active_user_count >= 2

    def test_a_revoked_device_is_not_counted(
        self, tenant_a, device_a, cashier_a
    ):
        from apps.accounts.models import Device

        device, _token = device_a
        with tenant_context(tenant_a.id):
            Device.objects.filter(pk=device.pk).update(is_active=False)

        rows = {row.slug: row for row in usage_summary(period_for(tenant_a))}

        assert rows["mama-njeri"].active_device_count == 0

    def test_the_endpoint_returns_totals_beside_the_rows(
        self, client_platform, client_cashier_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        body = client_platform.get(TRADING).json()

        assert body["totals"]["gross_cents"] == 18000
        assert body["totals"]["trading_tenant_count"] == 1

    def test_isolation_is_back_on_after_the_bypass(
        self, tenant_a, tenant_b, client_cashier_a, item_a, stock_a
    ):
        """The bypass covers the queries and nothing else."""
        from apps.core.tenancy import get_current_tenant_id

        sell(client_cashier_a, item_a)

        with tenant_context(tenant_b.id):
            usage_summary(period_for(tenant_a))
            # Still bound to B afterwards, not left wide open.
            assert get_current_tenant_id() == tenant_b.id

            from apps.sales.models import Sale

            assert Sale.objects.count() == 0
