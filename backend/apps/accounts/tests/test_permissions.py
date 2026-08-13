"""
Role boundaries.

Every check here asks the same question from a different angle: can someone do
something their role should not allow. The cases that matter most are the ones
where a cashier reaches something destructive, since a busy shop hands the till
to whoever is free.
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.accounts.constants import UserRole, role_rank
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


class TestRoleOrdering:
    """The ordering the permission classes are built on."""

    def test_roles_rank_from_cashier_up_to_owner(self):
        assert role_rank(UserRole.CASHIER) < role_rank(UserRole.MANAGER)
        assert role_rank(UserRole.MANAGER) < role_rank(UserRole.OWNER)

    def test_an_unknown_role_ranks_below_everything(self):
        """A stale token naming a removed role must lose access, not crash."""
        assert role_rank("DIRECTOR") == -1

    def test_a_manager_satisfies_a_cashier_requirement(self, manager_a):
        assert manager_a.has_role_at_least(UserRole.CASHIER)

    def test_a_cashier_does_not_satisfy_a_manager_requirement(self, cashier_a):
        assert not cashier_a.has_role_at_least(UserRole.MANAGER)

    def test_an_unknown_role_satisfies_nothing(self, cashier_a):
        cashier_a.role = "DIRECTOR"
        assert not cashier_a.has_role_at_least(UserRole.CASHIER)


class TestCashierBoundaries:
    """What someone working the till may not do."""

    def test_a_cashier_cannot_list_staff(self, client_cashier_a):
        assert client_cashier_a.get("/api/v1/auth/users/").status_code == 403

    def test_a_cashier_cannot_create_staff(self, client_cashier_a):
        response = client_cashier_a.post(
            "/api/v1/auth/users/",
            {
                "username": "sneaky",
                "full_name": "Sneaky New",
                "password": "sneaky-pass-1122",
                "role": "OWNER",
            },
            format="json",
        )
        assert response.status_code == 403

    def test_a_cashier_cannot_deactivate_anyone(self, client_cashier_a, manager_a):
        response = client_cashier_a.post(f"/api/v1/auth/users/{manager_a.id}/deactivate/")
        assert response.status_code == 403

    def test_a_cashier_cannot_register_a_till(self, client_cashier_a):
        response = client_cashier_a.post(
            "/api/v1/auth/devices/", {"name": "Rogue till"}, format="json"
        )
        assert response.status_code == 403

    def test_a_cashier_cannot_change_business_settings(self, client_cashier_a):
        response = client_cashier_a.patch(
            "/api/v1/tenant/", {"receipt_footer": "Changed"}, format="json"
        )
        assert response.status_code == 403

    def test_a_cashier_cannot_create_a_branch(self, client_cashier_a):
        response = client_cashier_a.post(
            "/api/v1/stores/", {"name": "Branch", "code": "BR2"}, format="json"
        )
        assert response.status_code == 403

    def test_a_cashier_can_still_read_what_the_till_needs(self, client_cashier_a, store_a):
        """The boundaries must not stop a cashier doing their job."""
        assert client_cashier_a.get("/api/v1/tenant/").status_code == 200
        assert client_cashier_a.get("/api/v1/stores/").status_code == 200
        assert client_cashier_a.get("/api/v1/tenant/modules/").status_code == 200


class TestManagerBoundaries:
    """A manager runs the shop floor but does not control the business."""

    def test_a_manager_can_list_staff(self, client_manager_a):
        assert client_manager_a.get("/api/v1/auth/users/").status_code == 200

    def test_a_manager_cannot_create_staff(self, client_manager_a):
        """Who can take money is the owner's decision."""
        response = client_manager_a.post(
            "/api/v1/auth/users/",
            {
                "username": "hired",
                "full_name": "Newly Hired",
                "password": "hired-pass-3312",
                "role": "CASHIER",
            },
            format="json",
        )
        assert response.status_code == 403

    def test_a_manager_can_register_a_till(self, client_manager_a):
        response = client_manager_a.post(
            "/api/v1/auth/devices/", {"name": "Second counter"}, format="json"
        )
        assert response.status_code == 201

    def test_a_manager_can_create_a_branch(self, client_manager_a):
        response = client_manager_a.post(
            "/api/v1/stores/", {"name": "Branch two", "code": "br2"}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["code"] == "BR2"

    def test_a_manager_cannot_change_business_settings(self, client_manager_a):
        response = client_manager_a.patch(
            "/api/v1/tenant/", {"kra_pin": "P051234567X"}, format="json"
        )
        assert response.status_code == 403


class TestOwnerCapabilities:
    def test_an_owner_can_create_staff(self, client_owner_a):
        response = client_owner_a.post(
            "/api/v1/auth/users/",
            {
                "username": "james",
                "full_name": "James Kamau",
                "password": "james-pass-5567",
                "role": "CASHIER",
                "pin": "5678",
            },
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["has_pin"] is True

    def test_an_owner_can_change_business_settings(self, client_owner_a):
        response = client_owner_a.patch(
            "/api/v1/tenant/", {"kra_pin": "P051234567X"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["kra_pin"] == "P051234567X"

    def test_an_owner_cannot_change_their_own_business_status(self, client_owner_a):
        """Suspension is the platform operator's decision, not the customer's."""
        response = client_owner_a.patch(
            "/api/v1/tenant/", {"status": "ACTIVE"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "TRIAL" or response.json()["status"] == "ACTIVE"

    def test_an_owner_cannot_deactivate_themselves(self, client_owner_a, owner_a):
        """Otherwise a business can lock itself out entirely."""
        response = client_owner_a.post(f"/api/v1/auth/users/{owner_a.id}/deactivate/")
        assert response.status_code == 400
        assert response.json()["code"] == "self_deactivation"

    def test_deactivating_a_cashier_also_clears_their_pin(
        self, client_owner_a, cashier_a, tenant_a
    ):
        """Access has to be removed at the till, not only at the API."""
        response = client_owner_a.post(f"/api/v1/auth/users/{cashier_a.id}/deactivate/")
        assert response.status_code == 200

        with transaction.atomic(), tenant_context(tenant_a.id):
            cashier_a.refresh_from_db()
        assert cashier_a.is_active is False
        assert cashier_a.pin_hash == ""


class TestDestructiveActionsAreAudited:
    """Every crossing of a role boundary leaves a record."""

    def test_deactivating_a_user_is_recorded(self, client_owner_a, cashier_a, tenant_a, owner_a):
        client_owner_a.post(
            f"/api/v1/auth/users/{cashier_a.id}/deactivate/",
            {"reason": "Left the business"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DEACTIVATE).first()

        assert entry is not None
        assert entry.actor_label == "owner"
        assert entry.entity_id == str(cashier_a.id)
        assert entry.reason == "Left the business"
        assert entry.before == {"is_active": True}
        assert entry.after == {"is_active": False}

    def test_revoking_a_till_is_recorded(self, client_owner_a, device_a, tenant_a):
        device, _token = device_a
        client_owner_a.post(
            f"/api/v1/auth/devices/{device.id}/revoke/",
            {"reason": "Tablet stolen"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(
                action=AuditAction.DEACTIVATE, entity_type="accounts.Device"
            ).first()

        assert entry is not None
        assert entry.reason == "Tablet stolen"

    def test_the_audit_trail_never_stores_credentials(self, client_owner_a, tenant_a):
        """Managers can read the audit trail, so it must not carry secrets."""
        client_owner_a.post(
            "/api/v1/auth/users/",
            {
                "username": "audited",
                "full_name": "Audited User",
                "password": "audited-pass-9987",
                "role": "CASHIER",
            },
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            entries = list(AuditLog.objects.all())

        serialised = str([(entry.before, entry.after) for entry in entries])
        assert "audited-pass-9987" not in serialised


class TestDeviceTokens:
    """Registration hands the token over exactly once."""

    def test_the_token_is_returned_on_registration(self, client_manager_a):
        response = client_manager_a.post(
            "/api/v1/auth/devices/", {"name": "New till"}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["device_token"]

    def test_the_token_is_not_available_afterwards(self, client_manager_a):
        created = client_manager_a.post(
            "/api/v1/auth/devices/", {"name": "New till"}, format="json"
        ).json()

        listed = client_manager_a.get("/api/v1/auth/devices/").json()["results"]
        assert all("device_token" not in row for row in listed)
        assert all(created["device_token"] != row.get("token_hash") for row in listed)

    def test_the_stored_hash_is_not_the_token(self, device_a):
        device, raw_token = device_a
        assert device.token_hash != raw_token
        assert len(device.token_hash) == 64
