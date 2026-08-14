"""
Unapplied money is visible in the console.

Real money sitting unapplied needs a person to notice within hours, not at the
end of a reporting period - so suspect callbacks and sale discrepancies are in
the operator's console now rather than waiting for the reporting milestone.

Read-only throughout. These are ledger-adjacent records, and a console that
could edit them would be a way to alter the evidence of what happened.
"""

from __future__ import annotations

import pytest
from django.contrib import admin

from apps.payments.models import MpesaCallback, MpesaCredential
from apps.platform_admin.sites import platform_admin_site
from apps.sales.models import SaleDiscrepancy

pytestmark = pytest.mark.django_db


class TestTheyAreRegistered:
    @pytest.mark.parametrize("model", [MpesaCallback, SaleDiscrepancy, MpesaCredential])
    def test_the_model_appears_in_the_console(self, model):
        assert model in platform_admin_site._registry

    def test_they_are_not_on_the_default_admin_site(self):
        """Nothing appears in the operator's console by accident.

        Registering on the default site as well would put these behind a path
        that only requires is_staff, rather than is_platform_admin.
        """
        for model in (MpesaCallback, SaleDiscrepancy):
            assert model not in admin.site._registry


class TestNothingCanBeEditedFromTheConsole:
    @pytest.mark.parametrize("model", [MpesaCallback, SaleDiscrepancy, MpesaCredential])
    def test_no_adding(self, model, rf, platform_admin):
        model_admin = platform_admin_site._registry[model]
        request = rf.get("/")
        request.user = platform_admin

        assert model_admin.has_add_permission(request) is False

    @pytest.mark.parametrize("model", [MpesaCallback, SaleDiscrepancy, MpesaCredential])
    def test_no_changing(self, model, rf, platform_admin):
        model_admin = platform_admin_site._registry[model]
        request = rf.get("/")
        request.user = platform_admin

        assert model_admin.has_change_permission(request) is False

    @pytest.mark.parametrize("model", [MpesaCallback, SaleDiscrepancy, MpesaCredential])
    def test_no_deleting(self, model, rf, platform_admin):
        """A refused callback is evidence. It does not get tidied away."""
        model_admin = platform_admin_site._registry[model]
        request = rf.get("/")
        request.user = platform_admin

        assert model_admin.has_delete_permission(request) is False


class TestCredentialsAreNotOnDisplay:
    def test_the_secrets_are_not_among_the_fields(self):
        """They are encrypted at rest.

        A console that decrypted them onto a page would undo that for the sake
        of a field nobody needs to read.
        """
        model_admin = platform_admin_site._registry[MpesaCredential]

        assert "consumer_key" not in model_admin.fields
        assert "consumer_secret" not in model_admin.fields
        assert "passkey" not in model_admin.fields

    def test_what_is_shown_is_enough_to_support_a_business(self):
        model_admin = platform_admin_site._registry[MpesaCredential]

        assert "shortcode" in model_admin.fields
        assert "environment" in model_admin.fields
        assert "last_error" in model_admin.fields


class TestFilteringToWhatNeedsAttention:
    def test_callbacks_can_be_filtered_by_whether_they_are_resolved(self):
        model_admin = platform_admin_site._registry[MpesaCallback]
        names = [
            getattr(entry, "parameter_name", entry) for entry in model_admin.list_filter
        ]
        assert "state" in names

    def test_discrepancies_can_be_filtered_the_same_way(self):
        model_admin = platform_admin_site._registry[SaleDiscrepancy]
        names = [
            getattr(entry, "parameter_name", entry) for entry in model_admin.list_filter
        ]
        assert "state" in names

    def test_the_open_filter_selects_unresolved_rows(self, tenant_a, rf):
        """The only view worth acting on."""
        from django.db import transaction

        from apps.core.tenancy import bypass_rls, tenant_context

        with transaction.atomic(), tenant_context(tenant_a.id):
            open_row = SaleDiscrepancy.objects.create(
                tenant=tenant_a,
                kind=SaleDiscrepancy.Kind.NEGATIVE_STOCK,
                detail="Sugar went to -2",
            )
            SaleDiscrepancy.objects.create(
                tenant=tenant_a,
                kind=SaleDiscrepancy.Kind.NEGATIVE_STOCK,
                detail="Already dealt with",
                resolved_at="2026-08-14T09:00:00Z",
            )

        from apps.platform_admin.admin import OpenIssueFilter

        model_admin = platform_admin_site._registry[SaleDiscrepancy]
        request = rf.get("/?state=open")

        with transaction.atomic(), bypass_rls():
            queryset = SaleDiscrepancy.all_objects.all()
            filtered = OpenIssueFilter(
                request, {"state": ["open"]}, SaleDiscrepancy, model_admin
            ).queryset(request, queryset)
            ids = [row.pk for row in filtered]

        assert ids == [open_row.pk]


class TestTheConsoleSeesAcrossBusinesses:
    def test_a_discrepancy_from_any_business_is_visible(self, tenant_a, tenant_b):
        """The console runs with isolation lifted, which is the point of it."""
        from django.db import transaction

        from apps.core.tenancy import bypass_rls, tenant_context

        for tenant in (tenant_a, tenant_b):
            with transaction.atomic(), tenant_context(tenant.id):
                SaleDiscrepancy.objects.create(
                    tenant=tenant,
                    kind=SaleDiscrepancy.Kind.NEGATIVE_STOCK,
                    detail=f"From {tenant.slug}",
                )

        with transaction.atomic(), bypass_rls():
            assert SaleDiscrepancy.all_objects.count() == 2

    def test_a_business_still_cannot_see_another_s(self, tenant_a, tenant_b):
        from django.db import transaction

        from apps.core.tenancy import tenant_context

        with transaction.atomic(), tenant_context(tenant_a.id):
            SaleDiscrepancy.objects.create(
                tenant=tenant_a,
                kind=SaleDiscrepancy.Kind.NEGATIVE_STOCK,
                detail="Mine",
            )

        with transaction.atomic(), tenant_context(tenant_b.id):
            assert SaleDiscrepancy.all_objects.count() == 0
