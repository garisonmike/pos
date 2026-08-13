"""Onboarding a business and getting it ready to trade."""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.accounts.models import User
from apps.catalog.models import TaxRate
from apps.core.tenancy import bypass_rls, tenant_context
from apps.stores.models import Store
from apps.tenants.models import BusinessType, ModuleKey, TenantModule, VatMode
from apps.tenants.services import SetupAlreadyCompleted, complete_setup, provision_tenant
from apps.tenants.templates_registry import BUSINESS_TEMPLATES, module_defaults

pytestmark = pytest.mark.django_db

SETUP = "/api/v1/tenant/setup/"


class TestBusinessTemplates:
    """Templates are defaults, and every business type has a complete set."""

    def test_every_business_type_has_a_template(self):
        for business_type in BusinessType.values:
            assert business_type in BUSINESS_TEMPLATES

    def test_a_retail_shop_tracks_stock(self):
        defaults = module_defaults(BusinessType.RETAIL)
        assert defaults[ModuleKey.STOCK] is True
        assert defaults[ModuleKey.RESTAURANT] is False
        assert defaults[ModuleKey.APPOINTMENTS] is False

    def test_a_salon_does_not_track_stock_by_default(self):
        defaults = module_defaults(BusinessType.SALON)
        assert defaults[ModuleKey.APPOINTMENTS] is True
        assert defaults[ModuleKey.STOCK] is False

    def test_a_pharmacy_gets_batch_tracking_on_top_of_retail(self):
        defaults = module_defaults(BusinessType.PHARMACY)
        assert defaults[ModuleKey.STOCK] is True
        assert defaults[ModuleKey.PHARMACY_BATCHES] is True

    def test_a_restaurant_gets_tables_and_stock(self):
        defaults = module_defaults(BusinessType.RESTAURANT)
        assert defaults[ModuleKey.RESTAURANT] is True
        assert defaults[ModuleKey.STOCK] is True

    def test_every_module_gets_a_row_even_when_off(self):
        """"Disabled" and "never considered" must not be different states."""
        defaults = module_defaults(BusinessType.RETAIL)
        assert set(defaults) == set(ModuleKey.values)

    def test_an_unknown_business_type_falls_back_to_retail(self):
        assert module_defaults("SPACEPORT") == module_defaults(BusinessType.RETAIL)

    def test_the_templates_are_offered_over_the_api(self, client_owner_a):
        response = client_owner_a.get("/api/v1/tenant/business-templates/")
        assert response.status_code == 200
        assert {row["business_type"] for row in response.json()} == set(BusinessType.values)


class TestProvisioning:
    """What the platform operator creates when a customer signs up."""

    def test_a_business_gets_an_owner_and_its_modules(self):
        with transaction.atomic(), bypass_rls():
            tenant, owner = provision_tenant(
                name="Duka la Wema",
                business_type=BusinessType.RETAIL,
                owner_username="wema",
                owner_full_name="Wema Otieno",
                owner_password="wema-pass-4451",
            )

            assert owner.role == "OWNER"
            assert owner.tenant_id == tenant.id
            assert TenantModule.all_objects.filter(tenant=tenant).count() == len(
                ModuleKey.values
            )

    def test_the_slug_is_derived_from_the_name(self):
        with transaction.atomic(), bypass_rls():
            tenant, _owner = provision_tenant(
                name="Duka la Wema",
                business_type=BusinessType.RETAIL,
                owner_username="wema",
                owner_full_name="Wema Otieno",
                owner_password="wema-pass-4451",
            )
        assert tenant.slug == "duka-la-wema"

    def test_a_colliding_slug_gets_a_suffix(self, tenant_a):
        with transaction.atomic(), bypass_rls():
            tenant, _owner = provision_tenant(
                name="Mama Njeri Duka",
                business_type=BusinessType.RETAIL,
                owner_username="second",
                owner_full_name="Second Owner",
                owner_password="second-pass-7781",
            )
        assert tenant.slug == "mama-njeri-duka"  # differs from the existing 'mama-njeri'

    def test_a_new_business_starts_unconfigured(self, tenant_a):
        assert tenant_a.is_setup_complete is False


class TestSetupWizard:
    """The owner's first run, from the till."""

    def test_setup_creates_a_branch_and_a_tax_rate(self, client_owner_a, tenant_a):
        response = client_owner_a.post(
            SETUP,
            {
                "business_type": "RETAIL",
                "vat_mode": "INCLUSIVE",
                "store_name": "Main shop",
                "store_code": "main",
                "tax_rate_name": "VAT 16%",
                "tax_rate_bps": 1600,
                "tax_is_inclusive": True,
            },
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["is_setup_complete"] is True

        with transaction.atomic(), tenant_context(tenant_a.id):
            store = Store.objects.get(tenant=tenant_a)
            tax_rate = TaxRate.objects.get(tenant=tenant_a)

        assert store.code == "MAIN"
        assert store.is_default is True
        assert tax_rate.rate_bps == 1600
        assert tax_rate.is_inclusive is True
        assert tax_rate.is_default is True

    def test_setup_can_add_staff_with_pins(self, client_owner_a, tenant_a):
        response = client_owner_a.post(
            SETUP,
            {
                "staff": [
                    {
                        "username": "mary",
                        "full_name": "Mary Wanjiku",
                        "password": "mary-pass-6612",
                        "pin": "1234",
                        "role": "CASHIER",
                    },
                    {
                        "username": "peter",
                        "full_name": "Peter Omondi",
                        "password": "peter-pass-8823",
                        "role": "MANAGER",
                    },
                ]
            },
            format="json",
        )
        assert response.status_code == 200

        with transaction.atomic(), tenant_context(tenant_a.id):
            mary = User.objects.get(tenant=tenant_a, username="mary")
            peter = User.objects.get(tenant=tenant_a, username="peter")

        assert mary.check_pin("1234")
        assert mary.role == "CASHIER"
        assert peter.pin_hash == ""
        assert peter.role == "MANAGER"

    def test_setup_defaults_to_kenyan_retail(self, client_owner_a, tenant_a):
        """Tapping through without changing anything must produce a usable shop."""
        response = client_owner_a.post(SETUP, {}, format="json")
        assert response.status_code == 200

        with transaction.atomic(), tenant_context(tenant_a.id):
            tax_rate = TaxRate.objects.get(tenant=tenant_a)
            store = Store.objects.get(tenant=tenant_a)

        assert tax_rate.rate_bps == 1600
        assert tax_rate.is_inclusive is True
        assert store.code == "MAIN"

    def test_setup_runs_only_once(self, client_owner_a):
        assert client_owner_a.post(SETUP, {}, format="json").status_code == 200

        second = client_owner_a.post(SETUP, {}, format="json")
        assert second.status_code == 409
        assert second.json()["code"] == "setup_already_completed"

    def test_a_second_run_does_not_create_a_duplicate_branch(self, client_owner_a, tenant_a):
        client_owner_a.post(SETUP, {}, format="json")
        client_owner_a.post(SETUP, {}, format="json")

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Store.objects.filter(tenant=tenant_a).count() == 1

    def test_changing_the_business_type_at_setup_reapplies_modules(
        self, client_owner_a, tenant_a
    ):
        """The operator's guess at sign-up is corrected by the owner's choice."""
        response = client_owner_a.post(
            SETUP, {"business_type": "PHARMACY"}, format="json",
        )
        assert response.status_code == 200

        with transaction.atomic(), tenant_context(tenant_a.id):
            batches = TenantModule.objects.get(
                tenant=tenant_a, module_key=ModuleKey.PHARMACY_BATCHES
            )
        assert batches.is_enabled is True

    def test_duplicate_usernames_in_the_payload_are_refused(self, client_owner_a):
        response = client_owner_a.post(
            SETUP,
            {
                "staff": [
                    {"username": "mary", "full_name": "Mary One", "password": "mary-pass-6612"},
                    {"username": "mary", "full_name": "Mary Two", "password": "mary-pass-7723"},
                ]
            },
            format="json",
        )
        assert response.status_code == 400

    def test_a_username_already_in_the_business_is_refused(self, client_owner_a, cashier_a):
        response = client_owner_a.post(
            SETUP,
            {
                "staff": [
                    {"username": "mary", "full_name": "Mary Again", "password": "mary-pass-6612"}
                ]
            },
            format="json",
        )
        assert response.status_code == 400

    def test_a_username_used_by_another_business_is_allowed(self, client_owner_a, cashier_b):
        """Two shops may each employ a Mary."""
        response = client_owner_a.post(
            SETUP,
            {
                "staff": [
                    {"username": "mary", "full_name": "Our Mary", "password": "mary-pass-6612"}
                ]
            },
            format="json",
        )
        assert response.status_code == 200

    def test_a_cashier_cannot_run_setup(self, client_cashier_a):
        assert client_cashier_a.post(SETUP, {}, format="json").status_code == 403

    def test_a_manager_cannot_run_setup(self, client_manager_a):
        assert client_manager_a.post(SETUP, {}, format="json").status_code == 403

    def test_the_service_refuses_a_second_run_directly(self, tenant_a, owner_a):
        """The guard lives in the service, not only in the view."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            complete_setup(
                tenant=tenant_a,
                business_type=BusinessType.RETAIL,
                vat_mode=VatMode.INCLUSIVE,
                store_name="Main",
                store_code="MAIN",
                tax_rate_name="VAT 16%",
                tax_rate_bps=1600,
                tax_is_inclusive=True,
            )

        tenant_a.refresh_from_db()
        with pytest.raises(SetupAlreadyCompleted):
            complete_setup(
                tenant=tenant_a,
                business_type=BusinessType.RETAIL,
                vat_mode=VatMode.INCLUSIVE,
                store_name="Main",
                store_code="MAIN",
                tax_rate_name="VAT 16%",
                tax_rate_bps=1600,
                tax_is_inclusive=True,
            )


class TestTenantSettings:
    def test_an_owner_reads_their_own_business(self, client_owner_a, tenant_a):
        response = client_owner_a.get("/api/v1/tenant/")
        assert response.status_code == 200
        assert response.json()["slug"] == tenant_a.slug

    def test_receipt_branding_can_be_changed(self, client_owner_a):
        response = client_owner_a.patch(
            "/api/v1/tenant/",
            {"receipt_header": "Mama Njeri Duka", "receipt_footer": "Karibu tena"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["receipt_footer"] == "Karibu tena"

    def test_the_slug_cannot_be_changed_by_the_business(self, client_owner_a, tenant_a):
        """Devices are set up with the slug; changing it would lock out every till."""
        response = client_owner_a.patch("/api/v1/tenant/", {"slug": "new-slug"}, format="json")
        assert response.status_code == 200
        assert response.json()["slug"] == tenant_a.slug

    def test_modules_are_listed(self, client_cashier_a):
        response = client_cashier_a.get("/api/v1/tenant/modules/")
        assert response.status_code == 200
        assert len(response.json()) == len(ModuleKey.values)
