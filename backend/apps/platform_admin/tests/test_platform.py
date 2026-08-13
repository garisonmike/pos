"""
The platform operator's surface.

This is the only place in the system where one request can see more than one
business, so these tests care about two things: that it works for the operator,
and that nobody else can reach it.
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.accounts.models import User
from apps.core.middleware import clear_tenant_status_cache
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import bypass_rls

pytestmark = pytest.mark.django_db

TENANTS = "/api/v1/platform/tenants/"
USAGE = "/api/v1/platform/usage/"


class TestPlatformSignIn:
    def test_the_operator_can_sign_in(self, anon_client, platform_admin):
        response = anon_client.post(
            "/api/v1/platform/auth/login/",
            {"username": "platform-op", "password": "platform-pass-9271"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["access"]

    def test_the_token_marks_them_as_a_platform_admin(self, anon_client, platform_admin):
        from rest_framework_simplejwt.tokens import AccessToken

        response = anon_client.post(
            "/api/v1/platform/auth/login/",
            {"username": "platform-op", "password": "platform-pass-9271"},
            format="json",
        )
        token = AccessToken(response.json()["access"])

        assert token["is_platform_admin"] is True
        assert token["tenant_id"] is None

    def test_a_tenant_user_cannot_sign_in_here(self, anon_client, tenant_a, owner_a):
        """The two sign-in surfaces must not overlap."""
        response = anon_client.post(
            "/api/v1/platform/auth/login/",
            {"username": "owner", "password": "owner-pass-8812"},
            format="json",
        )
        assert response.status_code == 400

    def test_the_operator_cannot_sign_in_at_the_tenant_endpoint(
        self, anon_client, platform_admin, tenant_a
    ):
        response = anon_client.post(
            "/api/v1/auth/login/",
            {
                "tenant_slug": tenant_a.slug,
                "username": "platform-op",
                "password": "platform-pass-9271",
            },
            format="json",
        )
        assert response.status_code == 400


class TestOnboarding:
    def test_the_operator_can_onboard_a_business(self, client_platform):
        response = client_platform.post(
            TENANTS,
            {
                "name": "Salon Sasa",
                "business_type": "SALON",
                "owner_username": "sasa",
                "owner_full_name": "Sasa Achieng",
                "owner_password": "sasa-pass-3391",
            },
            format="json",
        )
        assert response.status_code == 201

        body = response.json()
        assert body["slug"] == "salon-sasa"
        assert body["status"] == "TRIAL"
        assert body["owner"]["username"] == "sasa"

    def test_the_new_owner_can_immediately_sign_in(self, client_platform, anon_client):
        client_platform.post(
            TENANTS,
            {
                "name": "Salon Sasa",
                "business_type": "SALON",
                "owner_username": "sasa",
                "owner_full_name": "Sasa Achieng",
                "owner_password": "sasa-pass-3391",
            },
            format="json",
        )
        response = anon_client.post(
            "/api/v1/auth/login/",
            {"tenant_slug": "salon-sasa", "username": "sasa", "password": "sasa-pass-3391"},
            format="json",
        )
        assert response.status_code == 200

    def test_a_duplicate_slug_is_refused(self, client_platform, tenant_a):
        response = client_platform.post(
            TENANTS,
            {
                "name": "Another Shop",
                "slug": tenant_a.slug,
                "owner_username": "another",
                "owner_full_name": "Another Owner",
                "owner_password": "another-pass-4412",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_a_weak_owner_password_is_refused(self, client_platform):
        response = client_platform.post(
            TENANTS,
            {
                "name": "Weak Shop",
                "owner_username": "weak",
                "owner_full_name": "Weak Owner",
                "owner_password": "1234",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_onboarding_is_recorded_in_the_audit_trail(self, client_platform):
        client_platform.post(
            TENANTS,
            {
                "name": "Audited Shop",
                "owner_username": "audited",
                "owner_full_name": "Audited Owner",
                "owner_password": "audited-pass-5523",
            },
            format="json",
        )
        with transaction.atomic(), bypass_rls():
            entry = AuditLog.all_objects.filter(
                action=AuditAction.CREATE, entity_type="tenants.Tenant"
            ).first()

        assert entry is not None
        assert entry.actor_label == "platform-op"
        assert entry.tenant_id is None


class TestSuspension:
    def test_the_operator_can_suspend_a_business(self, client_platform, tenant_a):
        response = client_platform.post(
            f"{TENANTS}{tenant_a.id}/suspend/", {"reason": "Unpaid invoice"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "SUSPENDED"

    def test_a_suspended_businesss_till_is_refused(self, client_platform, tenant_a, client_owner_a):
        """The whole point: suspension stops the shop trading."""
        assert client_owner_a.get("/api/v1/stores/").status_code == 200

        client_platform.post(
            f"{TENANTS}{tenant_a.id}/suspend/", {"reason": "Unpaid invoice"}, format="json"
        )

        blocked = client_owner_a.get("/api/v1/stores/")
        assert blocked.status_code == 402
        assert blocked.json()["code"] == "tenant_inactive"

    def test_suspension_does_not_delete_anything(self, client_platform, tenant_a, store_a):
        client_platform.post(f"{TENANTS}{tenant_a.id}/suspend/", {}, format="json")

        with transaction.atomic(), bypass_rls():
            from apps.stores.models import Store

            assert Store.all_objects.filter(tenant=tenant_a).exists()

    def test_reactivation_restores_access(self, client_platform, tenant_a, client_owner_a):
        client_platform.post(f"{TENANTS}{tenant_a.id}/suspend/", {}, format="json")
        assert client_owner_a.get("/api/v1/stores/").status_code == 402

        client_platform.post(f"{TENANTS}{tenant_a.id}/reactivate/", {}, format="json")
        clear_tenant_status_cache(tenant_a.id)

        assert client_owner_a.get("/api/v1/stores/").status_code == 200

    def test_suspension_is_recorded_with_its_reason(self, client_platform, tenant_a):
        client_platform.post(
            f"{TENANTS}{tenant_a.id}/suspend/",
            {"reason": "Three months unpaid"},
            format="json",
        )
        with transaction.atomic(), bypass_rls():
            entry = AuditLog.all_objects.filter(action=AuditAction.SUSPEND).first()

        assert entry is not None
        assert entry.reason == "Three months unpaid"
        assert entry.before == {"status": "ACTIVE"}
        assert entry.after == {"status": "SUSPENDED"}

    def test_a_business_cannot_be_deleted_over_the_api(self, client_platform, tenant_a):
        response = client_platform.delete(f"{TENANTS}{tenant_a.id}/")
        assert response.status_code == 405


class TestUsageReporting:
    def test_usage_lists_every_business(self, client_platform, tenant_a, tenant_b):
        response = client_platform.get(USAGE)
        assert response.status_code == 200

        slugs = {row["slug"] for row in response.json()}
        assert {tenant_a.slug, tenant_b.slug} <= slugs

    def test_usage_counts_what_billing_needs(
        self, client_platform, tenant_a, cashier_a, store_a, device_a, item_a
    ):
        response = client_platform.get(USAGE)
        row = next(r for r in response.json() if r["slug"] == tenant_a.slug)

        assert row["user_count"] == 2  # owner and cashier
        assert row["active_user_count"] == 2
        assert row["store_count"] == 1
        assert row["device_count"] == 1
        assert row["item_count"] == 1
        assert "stock" in row["enabled_modules"]

    def test_usage_counts_do_not_bleed_between_businesses(
        self, client_platform, tenant_a, tenant_b, store_a, cashier_a
    ):
        """The one place cross-tenant reads are allowed still has to count right."""
        response = client_platform.get(USAGE)
        rows = {row["slug"]: row for row in response.json()}

        assert rows[tenant_a.slug]["store_count"] == 1
        assert rows[tenant_b.slug]["store_count"] == 0
        assert rows[tenant_a.slug]["user_count"] == 2
        assert rows[tenant_b.slug]["user_count"] == 1

    def test_the_operator_can_read_a_businesss_staff_for_support(
        self, client_platform, tenant_a, cashier_a
    ):
        response = client_platform.get(f"{TENANTS}{tenant_a.id}/users/")
        assert response.status_code == 200

        usernames = {row["username"] for row in response.json()}
        assert usernames == {"owner", "mary"}


class TestPlatformAdminAccount:
    """Constraints on the account that can reach across businesses."""

    def test_a_platform_admin_has_no_business(self, platform_admin):
        assert platform_admin.tenant_id is None
        assert platform_admin.is_platform_admin is True

    def test_a_platform_admin_cannot_be_given_a_business(self, platform_admin, tenant_a):
        """Enforced by a database constraint, not only by convention."""
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            with transaction.atomic(), bypass_rls():
                platform_admin.tenant = tenant_a
                platform_admin.save(update_fields=["tenant"])

    def test_an_owner_is_not_a_platform_admin(self, owner_a):
        assert owner_a.is_platform_admin is False

    def test_the_seed_command_is_idempotent(self, platform_admin):
        """Restarting the stack must not reset a password the operator changed."""
        from django.core.management import call_command

        platform_admin.set_password("changed-since-8891")
        with transaction.atomic(), bypass_rls():
            platform_admin.save(update_fields=["password"])

        call_command("ensure_platform_admin", username="platform-op", password="seed-pass-1122")

        with transaction.atomic(), bypass_rls():
            reloaded = User.all_objects.get(username="platform-op", tenant__isnull=True)
        assert reloaded.check_password("changed-since-8891")


class TestPlatformConsole:
    """The Django admin mounted as the operator's console."""

    def test_the_console_is_not_at_the_default_admin_path(self, anon_client):
        assert anon_client.get("/admin/").status_code == 404

    def test_the_console_requires_a_platform_admin(self, tenant_a):
        """A business owner is staff of their own shop, and must not reach here."""
        from django.conf import settings
        from django.test import Client

        from apps.platform_admin.sites import platform_admin_site

        class FakeRequest:
            user = None

        request = FakeRequest()
        request.user = User.all_objects.filter(tenant=tenant_a).first()
        assert platform_admin_site.has_permission(request) is False

        assert settings.PLATFORM_ADMIN_URL != "admin/"
        assert Client() is not None
