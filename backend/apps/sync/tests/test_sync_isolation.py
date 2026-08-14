"""
One business's till may not reach another business's data through sync.

Sync is the widest surface in the system: it accepts identifiers a till chose
for itself, replays them in bulk, and does so on behalf of a user who is not
watching. Every one of those identifiers is a chance to name something that
belongs to somebody else.
"""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.accounts.models import Device
from apps.core.tenancy import tenant_context
from apps.sales.models import Sale, SaleDiscrepancy
from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

CATALOG = "/api/v1/sync/catalog/"


@pytest.fixture
def device_b(tenant_b):
    with tenant_context(tenant_b.id):
        return Device.issue(tenant=tenant_b, name="Kwa Baba counter")


@pytest.mark.django_db
class TestATillMayOnlySyncToItsOwnBusiness:
    def test_another_businesss_device_id_is_not_found(
        self, client_cashier_a, device_b, item_a, stock_a
    ):
        """Not 'compared and rejected' - the lookup is tenant-scoped, so the
        row is simply absent. There is no cross-tenant branch to get wrong."""
        other_device, _token = device_b

        response = client_cashier_a.post(
            SYNC, batch(other_device, [offline_sale(item_a)]), format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unknown_device"

    def test_a_batch_naming_another_businesss_device_writes_no_sales(
        self, client_cashier_a, cashier_a, device_b, item_a, stock_a
    ):
        other_device, _token = device_b
        client_cashier_a.post(
            SYNC, batch(other_device, [offline_sale(item_a)]), format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 0

    def test_the_attempt_is_recorded_against_the_business_that_made_it(
        self, client_cashier_a, cashier_a, tenant_b, device_b, item_a, stock_a
    ):
        other_device, _token = device_b
        client_cashier_a.post(
            SYNC, batch(other_device, [offline_sale(item_a)]), format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            assert SaleDiscrepancy.objects.filter(
                kind=SaleDiscrepancy.Kind.UNKNOWN_DEVICE
            ).count() == 1

        # Filed against the shop whose user made the request, not the shop that
        # owns the device named. The other business did nothing.
        with tenant_context(tenant_b.id):
            assert SaleDiscrepancy.objects.count() == 0

    def test_a_sale_naming_another_businesss_item_is_rejected(
        self, client_cashier_a, cashier_a, device_a, store_a, item_b
    ):
        """The catalogue lookup is tenant-scoped, so the item is not found -
        which is the same refusal an item id that never existed would get."""
        device, _token = device_a
        payload = offline_sale(item_b)

        response = client_cashier_a.post(SYNC, batch(device, [payload]), format="json")

        assert response.json()["results"][0]["status"] == "rejected"
        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 0

    def test_the_other_business_never_sees_the_sale(
        self, client_cashier_a, tenant_b, device_a, item_a, stock_a
    ):
        device, _token = device_a
        client_cashier_a.post(SYNC, batch(device, [offline_sale(item_a)]), format="json")

        with tenant_context(tenant_b.id):
            assert Sale.objects.count() == 0


@pytest.mark.django_db
class TestTheIdempotencyKeyIsScopedPerBusiness:
    def test_two_businesses_may_use_the_same_client_uuid(
        self,
        client_cashier_a,
        cashier_a,
        client_owner_b,
        owner_b,
        device_a,
        device_b,
        item_a,
        stock_a,
        item_b,
        stock_b,
    ):
        """A till's identifier must not be able to suppress another shop's sale.

        The constraint is ``UNIQUE(tenant, client_uuid)`` rather than
        ``UNIQUE(client_uuid)`` precisely so that a collision between two
        businesses - whether by chance or on purpose - cannot make one shop's
        sale silently vanish as somebody else's duplicate.
        """
        shared = uuid.uuid4()
        device, _token = device_a
        other_device, _other_token = device_b

        first = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a, client_uuid=shared)]), format="json"
        ).json()
        second = client_owner_b.post(
            SYNC,
            batch(other_device, [offline_sale(item_b, client_uuid=shared, tendered=25000)]),
            format="json",
        ).json()

        assert first["results"][0]["status"] == "accepted"
        assert second["results"][0]["status"] == "accepted"
        assert first["results"][0]["sale_id"] != second["results"][0]["sale_id"]

    def test_each_business_keeps_its_own_receipt_series(
        self,
        client_cashier_a,
        client_owner_b,
        device_a,
        device_b,
        item_a,
        stock_a,
        item_b,
        stock_b,
    ):
        device, _token = device_a
        other_device, _other = device_b

        a = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a)]), format="json"
        ).json()
        b = client_owner_b.post(
            SYNC, batch(other_device, [offline_sale(item_b, tendered=25000)]), format="json"
        ).json()

        assert a["results"][0]["receipt_number"] == 1
        assert b["results"][0]["receipt_number"] == 1


@pytest.mark.django_db
class TestTheCatalogueDownloadStaysInsideOneBusiness:
    def test_a_till_downloads_only_its_own_items(
        self, client_cashier_a, item_a, item_b
    ):
        response = client_cashier_a.get(CATALOG)

        assert response.status_code == 200
        names = {row["name"] for row in response.json()["items"]}
        assert "Sugar 1kg" in names
        assert "Nails 2 inch" not in names

    def test_a_till_downloads_only_its_own_staff(
        self, client_cashier_a, cashier_a, cashier_b, owner_b
    ):
        response = client_cashier_a.get(CATALOG)

        usernames = {row["username"] for row in response.json()["staff"]}
        # Both businesses employ a 'mary'. Exactly one row comes back, and the
        # id proves which.
        ids = {row["id"] for row in response.json()["staff"] if row["username"] == "mary"}
        assert ids == {str(cashier_a.id)}
        assert len(usernames) == len(
            {row["username"] for row in response.json()["staff"]}
        )

    def test_the_catalogue_needs_authentication(self, anon_client):
        assert anon_client.get(CATALOG).status_code == 401


@pytest.mark.django_db
class TestWhatTheCatalogueSendsDown:
    def test_a_managers_pin_hash_is_sent_so_an_offline_check_can_run(
        self, client_cashier_a, manager_a
    ):
        with tenant_context(manager_a.tenant_id):
            manager_a.set_pin("7788")
            manager_a.save()

        rows = {row["username"]: row for row in client_cashier_a.get(CATALOG).json()["staff"]}

        assert rows["mngr"]["pin_hash"]
        assert rows["mngr"]["pin_version"] == 1

    def test_a_cashiers_pin_hash_is_never_sent(self, client_cashier_a, cashier_a):
        """No offline check would ever consult it, so downloading it would
        widen what a stolen tablet gives up for nothing in return."""
        rows = {row["username"]: row for row in client_cashier_a.get(CATALOG).json()["staff"]}

        assert rows["mary"]["pin_hash"] == ""
        # The version still travels, because it costs nothing and keeps the
        # shape of every row the same.
        assert "pin_version" in rows["mary"]

    def test_the_pin_version_moves_when_the_pin_does(
        self, client_cashier_a, manager_a
    ):
        with tenant_context(manager_a.tenant_id):
            manager_a.set_pin("7788")
            manager_a.save()
        before = {r["username"]: r for r in client_cashier_a.get(CATALOG).json()["staff"]}

        with tenant_context(manager_a.tenant_id):
            manager_a.set_pin("1122")
            manager_a.save()
        after = {r["username"]: r for r in client_cashier_a.get(CATALOG).json()["staff"]}

        assert after["mngr"]["pin_version"] == before["mngr"]["pin_version"] + 1

    def test_an_items_tax_travels_flattened_for_offline_pricing(
        self, client_cashier_a, item_a, tax_rate_a
    ):
        row = next(
            r for r in client_cashier_a.get(CATALOG).json()["items"] if r["sku"] == "SUGAR-1KG"
        )

        assert row["price_cents"] == 18000
        assert row["tax_rate_bps"] == 1600
        assert row["tax_is_inclusive"] is True

    def test_a_withdrawn_item_is_sent_with_its_flag_not_omitted(
        self, client_cashier_a, item_a
    ):
        """A till that never hears about a withdrawal keeps selling the thing."""
        with tenant_context(item_a.tenant_id):
            item_a.is_active = False
            item_a.save()

        row = next(
            r for r in client_cashier_a.get(CATALOG).json()["items"] if r["sku"] == "SUGAR-1KG"
        )
        assert row["is_active"] is False


@pytest.mark.django_db
class TestDownloadingOnlyWhatChanged:
    def test_a_second_download_since_the_first_brings_nothing_new(
        self, client_cashier_a, item_a
    ):
        first = client_cashier_a.get(CATALOG).json()
        second = client_cashier_a.get(CATALOG, {"since": first["server_time"]}).json()

        assert first["items"]
        assert second["items"] == []

    def test_a_changed_price_comes_back_on_the_next_download(
        self, client_cashier_a, item_a
    ):
        first = client_cashier_a.get(CATALOG).json()

        with tenant_context(item_a.tenant_id):
            item_a.price_cents = 19500
            item_a.save()

        second = client_cashier_a.get(CATALOG, {"since": first["server_time"]}).json()

        assert [row["price_cents"] for row in second["items"]] == [19500]

    def test_the_window_is_the_servers_clock_not_the_tills(
        self, client_cashier_a, item_a
    ):
        """A till whose clock runs fast would otherwise ask for a window that
        skips changes it never saw."""
        response = client_cashier_a.get(CATALOG).json()
        server_time = timezone.datetime.fromisoformat(response["server_time"])

        assert abs((timezone.now() - server_time).total_seconds()) < 60

    def test_an_unreadable_since_is_refused_rather_than_ignored(
        self, client_cashier_a, item_a
    ):
        """Ignoring it would send a full catalogue where an incremental one was
        asked for, which looks like it worked."""
        response = client_cashier_a.get(CATALOG, {"since": "yesterday"})

        assert response.status_code == 400
        assert response.json()["code"] == "bad_since"
