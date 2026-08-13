"""
The platform console's cross-tenant read boundary, asserted in both directions.

This is the one place in the system where a single request may see more than one
business, so it deserves a file that states the whole boundary in one place
rather than leaving it implied across several.

Both directions are here on purpose. A test that only proved the platform
operator can read everything would still pass if isolation were switched off
entirely; a test that only proved a business sees its own rows would still pass
if the console were broken. The pair is what pins the boundary down.

The mechanism, stated once so the assertions below read as consequences of it:

* ``tenants_tenant`` carries no policy at all. It is the registry that isolation
  is *defined against*, and sign-in must read a business by slug before any
  business is bound - so isolating it against itself would be circular.
* Every other business-owned table has a policy that permits a row when either
  the bound tenant matches, or the session flag ``app.bypass_rls`` is on.
* ``TenantBindingMiddleware`` sets that flag for exactly two URL prefixes: the
  console and ``/api/v1/platform/``. Nothing else can turn it on.

See ARCHITECTURE.md, "How the platform reads across businesses".
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction

from apps.accounts.models import User
from apps.catalog.models import Item
from apps.core.db.rls import POLICY_NAME
from apps.core.tenancy import bypass_rls, get_current_tenant_id, tenant_context
from apps.stores.models import Store
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_businesses_with_stock(tenant_a, tenant_b, item_a, store_a, store_b):
    """One item and one branch in each of two unrelated businesses."""
    with transaction.atomic(), tenant_context(tenant_b.id):
        Item.objects.create(
            tenant=tenant_b,
            sku="NAILS-2IN",
            name="Nails 2 inch",
            price_cents=25000,
        )
    return tenant_a, tenant_b


class TestTenantScopedReadsSeeOnlyTheirOwn:
    """Direction one: a business is confined to itself."""

    def test_a_bound_business_reads_only_its_own_items(self, two_businesses_with_stock):
        tenant_a, tenant_b = two_businesses_with_stock

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert [i.sku for i in Item.all_objects.all()] == ["SUGAR-1KG"]

        with transaction.atomic(), tenant_context(tenant_b.id):
            assert [i.sku for i in Item.all_objects.all()] == ["NAILS-2IN"]

    def test_a_bound_business_reads_only_its_own_staff(self, cashier_a, cashier_b, tenant_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            usernames = set(User.all_objects.values_list("username", flat=True))
        assert usernames == {"owner", "mary"}

    def test_a_bound_business_reads_only_its_own_branches(self, store_a, store_b, tenant_b):
        with transaction.atomic(), tenant_context(tenant_b.id):
            assert list(Store.all_objects.values_list("id", flat=True)) == [store_b.id]

    def test_the_api_confines_a_business_to_itself(
        self, client_owner_a, two_businesses_with_stock
    ):
        """The same boundary, through the whole stack rather than the ORM."""
        response = client_owner_a.get("/api/v1/stores/")
        assert response.status_code == 200
        assert len(response.json()["results"]) == 1


class TestPlatformScopedReadsSeeEverything:
    """Direction two: the console genuinely crosses businesses."""

    def test_bypass_reads_across_every_business(self, two_businesses_with_stock):
        with transaction.atomic(), bypass_rls():
            assert set(Item.all_objects.values_list("sku", flat=True)) == {
                "SUGAR-1KG",
                "NAILS-2IN",
            }

    def test_bypass_reads_staff_across_every_business(self, cashier_a, cashier_b):
        with transaction.atomic(), bypass_rls():
            marys = User.all_objects.filter(username="mary")
            assert marys.count() == 2
            assert len({user.tenant_id for user in marys}) == 2

    def test_the_platform_api_counts_across_businesses(
        self, client_platform, two_businesses_with_stock
    ):
        """What billing actually asks: one query, every business."""
        response = client_platform.get("/api/v1/platform/usage/")
        assert response.status_code == 200

        rows = {row["slug"]: row for row in response.json()}
        assert len(rows) >= 2
        assert rows["mama-njeri"]["item_count"] == 1
        assert rows["kwa-baba"]["item_count"] == 1

    def test_a_business_cannot_reach_the_platform_surface(self, client_owner_a):
        """The bypass is only ever available where a platform admin is required."""
        assert client_owner_a.get("/api/v1/platform/usage/").status_code == 403


class TestTheMechanismItself:
    """Assert the mechanism, so the documentation cannot quietly go stale."""

    def test_the_registry_table_carries_no_policy(self):
        """``tenants_tenant`` is deliberately unprotected.

        Sign-in resolves a business by slug before any business is bound, so a
        policy here would make it impossible to sign in at all. Access to this
        table is an application-layer concern instead: only the platform
        surfaces list it, and a business reads exactly one row, its own.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname = 'tenants_tenant'"
            )
            assert cursor.fetchone()[0] is False

    def test_business_owned_tables_do_carry_the_policy(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_policies WHERE policyname = %s", [POLICY_NAME]
            )
            protected = {row[0] for row in cursor.fetchall()}

        assert {"catalog_item", "accounts_user", "stores_store"} <= protected

    def test_the_registry_is_readable_without_any_business_bound(self, tenant_a):
        """The property sign-in depends on.

        Every other table returns nothing with no business bound. This one must
        not, or there would be no way to resolve a slug and bind one.
        """
        with transaction.atomic(), tenant_context(None):
            assert Tenant.objects.filter(slug=tenant_a.slug).exists()
            assert Store.all_objects.count() == 0  # everything else stays dark

    def test_bypass_does_not_survive_the_block(self, two_businesses_with_stock):
        """Crossing businesses is temporary and explicitly scoped."""
        with transaction.atomic():
            with bypass_rls():
                assert Item.all_objects.count() == 2
            assert Item.all_objects.count() == 0

    def test_bypass_nested_in_a_business_still_crosses(self, two_businesses_with_stock):
        """Ordering must not matter; the flag is an override, not a merge."""
        tenant_a, _tenant_b = two_businesses_with_stock
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Item.all_objects.count() == 1
            with bypass_rls():
                assert Item.all_objects.count() == 2
            assert Item.all_objects.count() == 1


class TestTheEscapeHatchForMigrationsAndBackfills:
    """The supported way to touch rows across businesses outside a request.

    A data migration or a one-off backfill has no request and therefore no
    bound business, which means it sees nothing at all - the safe default, but
    a confusing one to hit when a migration silently updates zero rows.

    ``bypass_rls()`` is the answer, and it works identically in a migration, a
    management command and a shell. The requirement is a transaction, because
    the underlying setting is transaction-scoped.
    """

    def test_a_backfill_can_read_and_write_across_businesses(
        self, two_businesses_with_stock
    ):
        with transaction.atomic(), bypass_rls():
            updated = Item.all_objects.all().update(cost_cents=1)
            assert updated == 2
            assert set(Item.all_objects.values_list("cost_cents", flat=True)) == {1}

    def test_a_backfill_without_the_hatch_touches_nothing(self, two_businesses_with_stock):
        """The failure this guards against: a migration that quietly does nothing.

        Not an error, which is why it is worth a test. A migration that reports
        success having updated zero rows is easy to miss until the data is
        wrong.
        """
        with transaction.atomic():
            assert Item.all_objects.all().update(cost_cents=99) == 0

    def test_a_per_business_backfill_can_loop(self, two_businesses_with_stock):
        """The alternative shape: bind each business in turn.

        Preferable when the work is genuinely per-business, because each
        iteration is confined to one and a bug cannot spill across them.
        """
        touched = {}
        for tenant_id in Tenant.objects.values_list("id", flat=True):
            with transaction.atomic(), tenant_context(tenant_id):
                touched[tenant_id] = Item.all_objects.count()
                assert get_current_tenant_id() == tenant_id

        # Two businesses, one item each, and each iteration saw only its own.
        assert len(touched) == 2
        assert sorted(touched.values()) == [1, 1]
