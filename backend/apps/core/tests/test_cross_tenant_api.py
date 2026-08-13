"""
Cross-tenant access, exercised through the API rather than the ORM.

The ORM-level tests prove the database refuses to hand over another business's
rows. These prove the same thing end to end: through the middleware that reads
the token, the permission classes, the viewsets and the serializers. That
matters because isolation could be undone at any of those layers - a view that
looks a record up by primary key without scoping, for instance - and the
database would then be asked for a row the caller is entitled to see under a
binding that was set from the wrong place.

The expected outcome for a cross-tenant lookup is 404, not 403. A 403 would
confirm the record exists, which tells one business something about another's
data even while refusing to show it.
"""

from __future__ import annotations

import pytest
from django.db import transaction
from django.urls import reverse

from apps.accounts.models import Device
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


class TestListEndpointsAreScoped:
    """A list request returns only the caller's own business's rows."""

    def test_staff_list_shows_only_own_business(self, client_owner_a, cashier_a, cashier_b):
        response = client_owner_a.get("/api/v1/auth/users/")
        assert response.status_code == 200

        usernames = {row["username"] for row in response.json()["results"]}
        assert "mary" in usernames  # this business's Mary
        assert len(usernames) == 2  # owner and mary, not the other Mary

    def test_store_list_shows_only_own_business(self, client_owner_a, store_a, store_b):
        response = client_owner_a.get("/api/v1/stores/")
        assert response.status_code == 200

        ids = {row["id"] for row in response.json()["results"]}
        assert str(store_a.id) in ids
        assert str(store_b.id) not in ids

    def test_device_list_shows_only_own_business(self, client_owner_a, device_a, tenant_b):
        with transaction.atomic(), tenant_context(tenant_b.id):
            Device.issue(tenant=tenant_b, name="Their till")

        response = client_owner_a.get("/api/v1/auth/devices/")
        assert response.status_code == 200

        names = {row["name"] for row in response.json()["results"]}
        assert names == {"Front counter"}


class TestDetailEndpointsRefuseOtherTenants:
    """Fetching another business's record by id must look like it never existed."""

    def test_reading_another_businesss_store(self, client_owner_a, store_b):
        response = client_owner_a.get(f"/api/v1/stores/{store_b.id}/")
        assert response.status_code == 404

    def test_updating_another_businesss_store(self, client_owner_a, store_b, tenant_b):
        response = client_owner_a.patch(
            f"/api/v1/stores/{store_b.id}/", {"name": "Taken over"}, format="json"
        )
        assert response.status_code == 404

        # Re-read inside business B's own context. Reading it from nowhere in
        # particular would be refused by the database, which is the behaviour
        # under test rather than a way to verify it.
        with transaction.atomic(), tenant_context(tenant_b.id):
            store_b.refresh_from_db()
        assert store_b.name == "Main"

    def test_reading_another_businesss_user(self, client_owner_a, cashier_b):
        response = client_owner_a.get(f"/api/v1/auth/users/{cashier_b.id}/")
        assert response.status_code == 404

    def test_deactivating_another_businesss_user(self, client_owner_a, cashier_b, tenant_b):
        response = client_owner_a.post(f"/api/v1/auth/users/{cashier_b.id}/deactivate/")
        assert response.status_code == 404

        with transaction.atomic(), tenant_context(tenant_b.id):
            cashier_b.refresh_from_db()
        assert cashier_b.is_active is True

    def test_setting_a_pin_on_another_businesss_user(self, client_owner_a, cashier_b):
        response = client_owner_a.post(
            f"/api/v1/auth/users/{cashier_b.id}/set-pin/", {"pin": "9999"}, format="json"
        )
        assert response.status_code == 404

    def test_revoking_another_businesss_device(self, client_owner_a, tenant_b):
        with transaction.atomic(), tenant_context(tenant_b.id):
            device, _token = Device.issue(tenant=tenant_b, name="Their till")

        response = client_owner_a.post(f"/api/v1/auth/devices/{device.id}/revoke/")
        assert response.status_code == 404

        with transaction.atomic(), tenant_context(tenant_b.id):
            device.refresh_from_db()
        assert device.is_active is True


class TestWritesCannotTargetAnotherTenant:
    """Supplying another business's identifier in a payload must not work."""

    def test_creating_a_user_ignores_a_supplied_tenant(self, client_owner_a, tenant_a, tenant_b):
        """The tenant comes from the token, never from the body."""
        response = client_owner_a.post(
            "/api/v1/auth/users/",
            {
                "username": "planted",
                "full_name": "Planted User",
                "password": "planted-pass-7741",
                "role": "CASHIER",
                "tenant": str(tenant_b.id),
            },
            format="json",
        )
        assert response.status_code == 201

        from apps.accounts.models import User

        with transaction.atomic(), tenant_context(tenant_a.id):
            planted = User.all_objects.get(username="planted")
        assert planted.tenant_id == tenant_a.id

    def test_creating_a_store_ignores_a_supplied_tenant(self, client_owner_a, tenant_a, tenant_b):
        response = client_owner_a.post(
            "/api/v1/stores/",
            {"name": "Branch two", "code": "TWO", "tenant": str(tenant_b.id)},
            format="json",
        )
        assert response.status_code == 201

        from apps.stores.models import Store

        with transaction.atomic(), tenant_context(tenant_a.id):
            created = Store.all_objects.get(code="TWO")
        assert created.tenant_id == tenant_a.id


class TestTokensAreNotPortableBetweenTenants:
    """A token issued for one business grants nothing in another."""

    def test_a_token_only_ever_reads_its_own_business(
        self, client_owner_a, client_owner_b, store_a, store_b
    ):
        """The same request, two tokens, two disjoint answers.

        Run back to back so that both requests are served on the same
        connection: this is the connection-reuse case, seen from the outside.
        """
        first = client_owner_a.get("/api/v1/stores/")
        second = client_owner_b.get("/api/v1/stores/")

        ids_a = {row["id"] for row in first.json()["results"]}
        ids_b = {row["id"] for row in second.json()["results"]}

        assert ids_a == {str(store_a.id)}
        assert ids_b == {str(store_b.id)}
        assert not ids_a & ids_b

    def test_own_settings_endpoint_returns_the_callers_business(
        self, client_owner_b, tenant_a, tenant_b
    ):
        response = client_owner_b.get("/api/v1/tenant/")
        assert response.status_code == 200
        assert response.json()["slug"] == tenant_b.slug


class TestPlatformSurfaceIsClosedToTenants:
    """The one prefix where isolation is lifted must be unreachable to tenants.

    Isolation is disabled for everything under /api/v1/platform/, so a tenant
    user reaching any route there would be able to read across the whole
    platform. Every route is checked, not a sample, because one unguarded view
    added later is all it would take.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/platform/tenants/",
            "/api/v1/platform/usage/",
        ],
    )
    def test_an_owner_is_refused(self, client_owner_a, path):
        response = client_owner_a.get(path)
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/platform/tenants/",
            "/api/v1/platform/usage/",
        ],
    )
    def test_an_anonymous_caller_is_refused(self, anon_client, path):
        response = anon_client.get(path)
        assert response.status_code == 401

    def test_an_owner_cannot_onboard_a_business(self, client_owner_a):
        response = client_owner_a.post(
            "/api/v1/platform/tenants/",
            {
                "name": "Self served",
                "owner_username": "self",
                "owner_full_name": "Self Served",
                "owner_password": "self-pass-9912",
            },
            format="json",
        )
        assert response.status_code == 403

    def test_an_owner_cannot_read_another_businesss_staff(self, client_owner_a, tenant_b):
        response = client_owner_a.get(f"/api/v1/platform/tenants/{tenant_b.id}/users/")
        assert response.status_code == 403

    def test_every_platform_route_requires_a_platform_admin(self):
        """Structural check: no route under the platform prefix may be open.

        Walks the URL conf rather than a hand-written list, so a route added
        later without the right permission class fails here instead of
        shipping.
        """
        from apps.platform_admin import urls as platform_urls

        # Sign-in and refresh must be reachable before the caller holds a
        # usable token. Both read only accounts with no tenant, so neither can
        # be used to reach a business's data.
        allowed_without_platform_admin = {"platform-login", "platform-token-refresh"}
        offenders = []

        for pattern in _flatten(platform_urls.urlpatterns):
            callback = pattern.callback
            view_class = getattr(callback, "cls", None) or getattr(
                callback, "view_class", None
            )
            if view_class is None:
                continue
            if pattern.name in allowed_without_platform_admin:
                continue

            permission_names = {
                permission.__name__
                for permission in getattr(view_class, "permission_classes", [])
            }
            if "IsPlatformAdmin" not in permission_names:
                offenders.append(f"{pattern.name or pattern.pattern} -> {view_class.__name__}")

        assert not offenders, (
            "Routes under /api/v1/platform/ run with tenant isolation lifted, so "
            f"each one must require IsPlatformAdmin. These do not: {offenders}"
        )


def _flatten(patterns):
    """Yield every concrete URL pattern, descending through includes."""
    from django.urls.resolvers import URLPattern, URLResolver

    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _flatten(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            yield pattern


def test_reverse_is_available_for_health():
    """Sanity check that the URL conf loaded, so the walk above means something."""
    assert reverse("health") == "/api/v1/health/"
