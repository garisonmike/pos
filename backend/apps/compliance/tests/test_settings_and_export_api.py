"""
The back-office surface, and the three hardenings that came with it.
"""

from __future__ import annotations

import csv
import io

import pytest

from apps.compliance.models import (
    ComplianceDocument,
    ComplianceMode,
    InvoiceCounter,
    SubmissionState,
)
from apps.compliance.services import issue_for_settled_sale, resolve_adapter
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context

SETTINGS = "/api/v1/compliance/settings/"
EXPORT = "/api/v1/compliance/export/"
DOCUMENTS = "/api/v1/compliance/documents/"
CHECKOUT = "/api/v1/sales/checkout/cash/"


def sell(client, item, **extra) -> dict:
    body = {
        "lines": [{"item_id": str(item.id), "quantity": "1"}],
        "tendered_cents": 18000,
    }
    body.update(extra)
    return client.post(CHECKOUT, body, format="json").json()


@pytest.fixture
def registered_tenant(tenant_a):
    with tenant_context(tenant_a.id):
        tenant_a.compliance_mode = ComplianceMode.MANUAL
        tenant_a.kra_pin = "P051234567X"
        tenant_a.save()
        return tenant_a


# ---------------------------------------------------------------------------
# 1. The stale-object hardening
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSettlementIsVerifiedFromTheDatabase:
    """The mistake the sync path made, made impossible for a fourth caller.

    ``take_cash`` settles its own re-fetched row under a lock, so whatever the
    caller is holding is routinely still OPEN with stale totals.
    """

    def _stale_sale(self, tenant, store, cashier, item):
        from apps.sales.services import LineRequest, create_sale, take_cash

        sale = create_sale(
            tenant=tenant,
            store=store,
            cashier=cashier,
            lines=[LineRequest(item_id=str(item.id), quantity=1)],
        )
        take_cash(sale=sale, tendered_cents=18000, user=cashier)
        # Deliberately NOT refreshed. This instance still says OPEN.
        return sale

    def test_a_stale_instance_still_gets_its_invoice(
        self, tenant_a, registered_tenant, store_a, cashier_a, item_a, stock_a
    ):
        from apps.sales.models import SaleState

        with tenant_context(tenant_a.id):
            sale = self._stale_sale(tenant_a, store_a, cashier_a, item_a)

            assert sale.state == SaleState.OPEN, "the fixture must actually be stale"

            document = issue_for_settled_sale(sale=sale)

        assert document is not None
        assert document.invoice_number == 1

    def test_the_frozen_figures_come_from_the_database_too(
        self, tenant_a, registered_tenant, store_a, cashier_a, item_a, stock_a
    ):
        """Not only the state check. A caller holding a stale instance would
        otherwise freeze stale totals onto a tax document."""
        with tenant_context(tenant_a.id):
            sale = self._stale_sale(tenant_a, store_a, cashier_a, item_a)
            # Corrupt the in-memory copy the way a stale read would.
            sale.total_cents = 1
            sale.tax_cents = 1
            sale.subtotal_cents = 1

            document = issue_for_settled_sale(sale=sale)

        assert document.gross_cents == 18000
        assert document.tax_cents == 2483

    def test_a_genuinely_unsettled_sale_is_still_refused(
        self, tenant_a, registered_tenant, store_a, cashier_a, item_a, stock_a
    ):
        """Reading from the database must not turn the guard off."""
        from apps.compliance.services import ComplianceError, issue_invoice
        from apps.sales.services import LineRequest, create_sale

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            with pytest.raises(ComplianceError) as exc:
                issue_invoice(sale=sale)

        assert exc.value.code == "sale_not_settled"


# ---------------------------------------------------------------------------
# 2. Unnumbered documents
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUnregisteredBusinessesTakeNoNumber:
    def test_the_document_is_kept_but_unnumbered(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert document.invoice_number is None
        assert document.invoice_code == ""
        assert document.is_numbered is False
        assert document.submission_state == SubmissionState.NOT_REQUIRED

    def test_the_counter_is_never_touched(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """Consistent with an offline sale having no number until it lands:
        a number is taken only when something will be filed against it."""
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            assert not InvoiceCounter.objects.filter(last_number__gt=0).exists()

    def test_registering_later_starts_the_series_at_one(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """The unnumbered documents did not consume anything, so the first real
        invoice is genuinely the first."""
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(tenant_a.id):
            tenant_a.compliance_mode = ComplianceMode.MANUAL
            tenant_a.kra_pin = "P051234567X"
            tenant_a.save()

        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert document.invoice_number == 1

    def test_several_unnumbered_documents_coexist(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """Postgres treats nulls as distinct, so the unique constraint on
        (tenant, invoice_number) still holds with many of them."""
        for _ in range(3):
            sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            assert ComplianceDocument.objects.filter(invoice_number=None).count() == 3

    def test_an_unnumbered_document_is_not_exported(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a, client_owner_a
    ):
        """It is a record that somebody asked, not part of any return. Filing
        it would declare something the business is not registered to declare."""
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        response = client_owner_a.get(EXPORT)
        rows = list(csv.reader(io.StringIO(response.content.decode())))

        assert len(rows) == 1  # header only

    def test_it_still_reads_sensibly(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert "unnumbered" in str(document)


# ---------------------------------------------------------------------------
# 3. The unknown-mode flag
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAnUnknownModeLeavesATrace:
    def _drift(self, tenant):
        from apps.tenants.models import Tenant

        # Written past the field's own validation, as a hand-edit or a
        # withdrawn regime would leave it.
        Tenant.objects.filter(pk=tenant.pk).update(compliance_mode="ETIMS_V9")
        tenant.refresh_from_db()
        return tenant

    def test_the_fallback_still_happens(self, tenant_a):
        with tenant_context(tenant_a.id):
            adapter = resolve_adapter(self._drift(tenant_a))

        # Unchanged behaviour: a drifted setting stops a shop submitting, not
        # selling.
        assert adapter.name == "null"

    def test_it_is_recorded_for_a_person(self, tenant_a):
        with tenant_context(tenant_a.id):
            resolve_adapter(self._drift(tenant_a))
            entry = AuditLog.objects.get(action=AuditAction.COMPLIANCE_MODE_UNKNOWN)

        assert entry.after["configured_mode"] == "ETIMS_V9"
        assert entry.after["fell_back_to"] == "null"
        assert "Nothing will be submitted" in entry.after["consequence"]

    def test_a_known_mode_records_nothing(self, tenant_a, registered_tenant):
        with tenant_context(tenant_a.id):
            resolve_adapter(registered_tenant)

            assert not AuditLog.objects.filter(
                action=AuditAction.COMPLIANCE_MODE_UNKNOWN
            ).exists()

    def test_every_affected_sale_leaves_its_own_entry(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """Not deduplicated. A condition that should never occur is worth being
        noisy about, and every mis-filed sale deserves its own record."""
        with tenant_context(tenant_a.id):
            self._drift(tenant_a)

        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            assert (
                AuditLog.objects.filter(
                    action=AuditAction.COMPLIANCE_MODE_UNKNOWN
                ).count()
                == 2
            )

    def test_the_shop_goes_on_selling(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        with tenant_context(tenant_a.id):
            self._drift(tenant_a)

        settled = sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        assert settled["state"] == "PAID"


# ---------------------------------------------------------------------------
# The settings endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReadingTheTaxSetup:
    def test_any_member_of_staff_may_read_it(
        self, client_cashier_a, registered_tenant
    ):
        """A till needs to know whether to ask for a buyer PIN at all."""
        response = client_cashier_a.get(SETTINGS)

        assert response.status_code == 200
        assert response.json()["compliance_mode"] == ComplianceMode.MANUAL

    def test_it_shows_where_the_series_has_reached(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        body = client_owner_a.get(SETTINGS).json()

        assert body["next_invoice_number"] == 2
        assert body["invoice_prefix"] == "TI"

    def test_an_unrecognised_mode_is_labelled_as_such(self, client_owner_a, tenant_a):
        from apps.tenants.models import Tenant

        Tenant.objects.filter(pk=tenant_a.pk).update(compliance_mode="ETIMS_V9")

        assert client_owner_a.get(SETTINGS).json()["mode_label"] == "Not recognised"

    def test_it_needs_authentication(self, anon_client):
        assert anon_client.get(SETTINGS).status_code == 401


@pytest.mark.django_db
class TestChangingTheTaxSetup:
    def test_an_owner_may_change_the_mode(self, client_owner_a, tenant_a):
        response = client_owner_a.patch(
            SETTINGS, {"compliance_mode": "MANUAL"}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["compliance_mode"] == "MANUAL"

    def test_a_manager_may_not(self, client_manager_a, tenant_a):
        """Above Manager deliberately. Getting this wrong means declaring tax
        that is not owed, or failing to declare tax that is - and that lands on
        the owner, not on whoever was managing that afternoon."""
        response = client_manager_a.patch(
            SETTINGS, {"compliance_mode": "MANUAL"}, format="json"
        )

        assert response.status_code == 403

    def test_a_cashier_may_not(self, client_cashier_a, tenant_a):
        assert (
            client_cashier_a.patch(
                SETTINGS, {"compliance_mode": "MANUAL"}, format="json"
            ).status_code
            == 403
        )

    def test_a_refused_change_does_not_happen(
        self, client_manager_a, cashier_a, tenant_a
    ):
        client_manager_a.patch(SETTINGS, {"compliance_mode": "MANUAL"}, format="json")

        with tenant_context(cashier_a.tenant_id):
            tenant_a.refresh_from_db()

        assert tenant_a.compliance_mode == "NONE"

    def test_the_change_is_audited_with_both_values(
        self, client_owner_a, owner_a, tenant_a
    ):
        client_owner_a.patch(SETTINGS, {"compliance_mode": "MANUAL"}, format="json")

        with tenant_context(owner_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.COMPLIANCE_MODE_CHANGED)

        assert entry.before["compliance_mode"] == "NONE"
        assert entry.after["compliance_mode"] == "MANUAL"
        assert entry.actor_id == owner_a.id

    def test_a_change_of_regime_has_its_own_action(
        self, client_owner_a, owner_a, tenant_a
    ):
        """Findable without reading every settings edit the business ever
        made."""
        client_owner_a.patch(SETTINGS, {"kra_pin": "P051234567X"}, format="json")

        with tenant_context(owner_a.tenant_id):
            assert not AuditLog.objects.filter(
                action=AuditAction.COMPLIANCE_MODE_CHANGED
            ).exists()
            assert AuditLog.objects.filter(action=AuditAction.UPDATE).exists()

    def test_an_unknown_mode_cannot_be_set_through_the_api(
        self, client_owner_a, tenant_a
    ):
        response = client_owner_a.patch(
            SETTINGS, {"compliance_mode": "ETIMS_V9"}, format="json"
        )

        assert response.status_code == 400

    def test_a_malformed_tax_pin_is_refused(self, client_owner_a, tenant_a):
        assert (
            client_owner_a.patch(
                SETTINGS, {"kra_pin": "nonsense"}, format="json"
            ).status_code
            == 400
        )

    def test_the_pin_is_normalised(self, client_owner_a, owner_a, tenant_a):
        client_owner_a.patch(SETTINGS, {"kra_pin": " p051234567x "}, format="json")

        with tenant_context(owner_a.tenant_id):
            tenant_a.refresh_from_db()

        assert tenant_a.kra_pin == "P051234567X"

    def test_the_prefix_may_be_set_before_the_series_starts(
        self, client_owner_a, tenant_a
    ):
        response = client_owner_a.patch(
            SETTINGS, {"invoice_prefix": "inv"}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["invoice_prefix"] == "INV"

    def test_the_prefix_is_frozen_once_invoices_exist(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        """Changing it mid-series would produce two spellings of one gapless
        sequence, and a filer could not tell whether anything was missing
        between them."""
        sell(client_cashier_a, item_a)

        response = client_owner_a.patch(
            SETTINGS, {"invoice_prefix": "XX"}, format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "series_already_started"

    def test_resending_the_same_prefix_is_not_a_change(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        """A settings screen that PATCHes the whole form must not trip over an
        unchanged field."""
        sell(client_cashier_a, item_a)

        response = client_owner_a.patch(
            SETTINGS, {"invoice_prefix": "TI", "kra_pin": "P051234567X"}, format="json"
        )

        assert response.status_code == 200

    def test_an_empty_prefix_is_refused(self, client_owner_a, tenant_a):
        assert (
            client_owner_a.patch(
                SETTINGS, {"invoice_prefix": "   "}, format="json"
            ).status_code
            == 400
        )

    def test_the_counter_cannot_be_moved_by_hand(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        """A counter that could be set is not a gapless series."""
        sell(client_cashier_a, item_a)

        client_owner_a.patch(SETTINGS, {"next_invoice_number": 500}, format="json")

        assert client_owner_a.get(SETTINGS).json()["next_invoice_number"] == 2


# ---------------------------------------------------------------------------
# The export endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDownloadingTheExport:
    def test_a_manager_may_download_it(
        self, client_manager_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        """The back office's own filing work. A manager doing the monthly
        return should not need the owner's account."""
        sell(client_cashier_a, item_a)

        response = client_manager_a.get(EXPORT)

        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"

    def test_a_cashier_may_not(self, client_cashier_a, registered_tenant):
        assert client_cashier_a.get(EXPORT).status_code == 403

    def test_it_comes_back_as_a_download(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        response = client_owner_a.get(EXPORT)

        assert "attachment" in response["Content-Disposition"]
        assert "mama-njeri" in response["Content-Disposition"]

    def test_the_rows_are_the_documents(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        rows = list(csv.reader(io.StringIO(client_owner_a.get(EXPORT).content.decode())))

        assert rows[0][0] == "Document"
        assert rows[1][8] == "180.00"

    def test_pdf_is_offered_too(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        response = client_owner_a.get(EXPORT + "pdf/")

        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_the_pdf_lives_on_its_own_path_not_a_format_parameter(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        """Pins the reason the route is split, because it is not obvious.

        DRF reserves ``format`` for its own content negotiation, and it does
        not merely ignore an unrecognised one - it answers **404**. So an
        export endpoint reading ``?format=pdf`` would look like a missing URL
        rather than a bad parameter, which is a confusing half-hour for whoever
        next tries it. The receipt endpoints are split for exactly this reason,
        and this file rediscovered it the same way.
        """
        sell(client_cashier_a, item_a)

        assert client_owner_a.get(EXPORT, {"format": "pdf"}).status_code == 404
        assert client_owner_a.get(EXPORT + "pdf/").status_code == 200

    def test_a_period_can_be_asked_for(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        from django.utils import timezone

        sell(client_cashier_a, item_a)
        cutoff = timezone.now().isoformat()
        sell(client_cashier_a, item_a)

        rows = list(
            csv.reader(
                io.StringIO(
                    client_owner_a.get(EXPORT, {"since": cutoff}).content.decode()
                )
            )
        )

        assert len(rows) == 2  # header plus one

    def test_an_unreadable_period_is_refused_rather_than_ignored(
        self, client_owner_a, registered_tenant
    ):
        """Silently dropping it would hand somebody a full export where they
        asked for one month, and it would look like it worked."""
        response = client_owner_a.get(EXPORT, {"since": "last Tuesday"})

        assert response.status_code == 400
        assert response.json()["code"] == "bad_since"

    def test_another_businesss_documents_are_not_in_it(
        self,
        client_owner_a,
        client_cashier_a,
        registered_tenant,
        client_owner_b,
        item_a,
        stock_a,
        item_b,
        stock_b,
        store_b,
    ):
        sell(client_cashier_a, item_a)

        rows = list(csv.reader(io.StringIO(client_owner_b.get(EXPORT).content.decode())))

        assert len(rows) == 1


@pytest.mark.django_db
class TestListingDocuments:
    def test_a_manager_may_list_them(
        self, client_manager_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        response = client_manager_a.get(DOCUMENTS)

        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_a_cashier_may_not(self, client_cashier_a, registered_tenant):
        assert client_cashier_a.get(DOCUMENTS).status_code == 403

    def test_they_can_be_filtered_by_state(
        self, client_owner_a, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        pending = client_owner_a.get(DOCUMENTS, {"state": "PENDING"}).json()
        submitted = client_owner_a.get(DOCUMENTS, {"state": "SUBMITTED"}).json()

        assert len(pending) == 1
        assert submitted == []

    def test_an_unknown_state_is_refused(self, client_owner_a, registered_tenant):
        response = client_owner_a.get(DOCUMENTS, {"state": "MAYBE"})

        assert response.status_code == 400

    def test_unnumbered_documents_are_listed(
        self, client_owner_a, client_cashier_a, tenant_a, item_a, stock_a
    ):
        """They are excluded from the *export*, not hidden from the shop."""
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        body = client_owner_a.get(DOCUMENTS).json()

        assert len(body) == 1
        assert body[0]["is_numbered"] is False

    def test_another_businesss_documents_are_invisible(
        self, client_owner_b, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        assert client_owner_b.get(DOCUMENTS).json() == []
