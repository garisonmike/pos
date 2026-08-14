"""
Raising a tax document, and refusing to change one afterwards.
"""

from __future__ import annotations

import pytest

from apps.compliance.models import (
    ComplianceDocument,
    ComplianceMode,
    DocumentKind,
    SubmissionState,
    looks_like_a_kra_pin,
)
from apps.compliance.numbering import allocate_invoice_number, peek_next_invoice_number
from apps.compliance.services import (
    ComplianceError,
    issue_credit_note,
    issue_for_settled_sale,
    issue_invoice,
    submit_pending,
    tax_breakdown_for,
)
from apps.core.tenancy import tenant_context
from apps.sales.models import Sale, SaleState

CHECKOUT = "/api/v1/sales/checkout/cash/"


@pytest.fixture
def registered_tenant(tenant_a):
    """A business that is registered for VAT and files by hand."""
    with tenant_context(tenant_a.id):
        tenant_a.compliance_mode = ComplianceMode.MANUAL
        tenant_a.kra_pin = "P051234567X"
        tenant_a.save()
        return tenant_a


def sell(client, item, *, tendered=18000, **extra) -> dict:
    body = {
        "lines": [{"item_id": str(item.id), "quantity": "1"}],
        "tendered_cents": tendered,
    }
    body.update(extra)
    return client.post(CHECKOUT, body, format="json").json()


@pytest.mark.django_db
class TestTheTaxPinShape:
    def test_a_well_formed_pin_is_accepted(self):
        assert looks_like_a_kra_pin("P051234567X")

    def test_lower_case_is_accepted(self):
        assert looks_like_a_kra_pin("p051234567x")

    def test_surrounding_space_is_forgiven(self):
        """A cashier copying a PIN off a card will paste the space with it."""
        assert looks_like_a_kra_pin("  P051234567X ")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "P05123456X",  # eight digits
            "P0512345678X",  # ten digits
            "0051234567X",  # leading digit
            "P0512345678",  # no trailing letter
            "P051234567XY",
            "not a pin at all",
        ],
    )
    def test_anything_else_is_not_a_pin(self, value):
        assert not looks_like_a_kra_pin(value)

    def test_the_check_is_shape_only_and_says_so(self):
        """Nothing here can verify a PIN without asking KRA. A field that looks
        verified and is not is worse than one that plainly is not."""
        from apps.compliance import models

        assert "shape" in models.looks_like_a_kra_pin.__doc__.lower()
        # A pin that is well-formed but belongs to nobody still passes.
        assert looks_like_a_kra_pin("Z999999999Z")


@pytest.mark.django_db
class TestInvoiceNumbering:
    def test_numbers_start_at_one(self, tenant_a):
        with tenant_context(tenant_a.id):
            number, code = allocate_invoice_number(tenant_a)

        assert number == 1
        assert code.startswith("TI-")
        assert code.endswith("-000001")

    def test_numbers_run_consecutively(self, tenant_a):
        with tenant_context(tenant_a.id):
            numbers = [allocate_invoice_number(tenant_a)[0] for _ in range(5)]

        assert numbers == [1, 2, 3, 4, 5]

    def test_each_business_keeps_its_own_series(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.id):
            allocate_invoice_number(tenant_a)
            allocate_invoice_number(tenant_a)
        with tenant_context(tenant_b.id):
            theirs, _code = allocate_invoice_number(tenant_b)

        assert theirs == 1

    def test_the_invoice_series_is_separate_from_the_receipt_series(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """A receipt number identifies what a customer was handed; an invoice
        number identifies a taxable document. Sharing one series would put gaps
        in the tax sequence the moment anything was voided."""
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert settled["receipt_number"] == 1
        assert document.invoice_number == 1
        assert document.invoice_code != settled["receipt_code"]

    def test_peeking_does_not_take_a_number(self, tenant_a):
        with tenant_context(tenant_a.id):
            allocate_invoice_number(tenant_a)
            peeked = peek_next_invoice_number(tenant_a)
            actual, _code = allocate_invoice_number(tenant_a)

        assert peeked == actual == 2

    def test_a_rolled_back_sale_leaves_no_gap(self, tenant_a):
        """The whole reason allocation happens inside the sale's transaction."""
        from django.db import transaction

        with tenant_context(tenant_a.id):
            allocate_invoice_number(tenant_a)

            try:
                with transaction.atomic():
                    allocate_invoice_number(tenant_a)
                    raise RuntimeError("the sale failed")
            except RuntimeError:
                pass

            after, _code = allocate_invoice_number(tenant_a)

        assert after == 2


@pytest.mark.django_db
class TestRaisingAnInvoice:
    def test_a_settled_sale_gets_one(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert document.kind == DocumentKind.INVOICE
        assert document.gross_cents == 18000
        assert document.seller_pin == "P051234567X"

    def test_the_figures_are_frozen_not_read_through_the_sale(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """A rate change next year must not restate what was declared."""
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])
            item_a.price_cents = 99999
            item_a.save()
            document.refresh_from_db()

        assert document.gross_cents == 18000

    def test_the_tax_is_broken_down_by_rate(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """A single total tells a filer nothing when a sale mixes rates, which
        a duka selling bread and sugar does constantly."""
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert document.tax_breakdown == [
            {"rate_bps": 1600, "net_cents": 15517, "tax_cents": 2483, "gross_cents": 18000}
        ]

    def test_a_buyer_pin_is_recorded_when_one_is_given(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a, buyer_pin="p012345678z")

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        # Normalised on the way in, so an export does not carry two spellings
        # of one PIN.
        assert document.buyer_pin == "P012345678Z"

    def test_a_malformed_buyer_pin_is_refused(
        self, client_cashier_a, registered_tenant, item_a, stock_a
    ):
        response = client_cashier_a.post(
            CHECKOUT,
            {
                "lines": [{"item_id": str(item_a.id), "quantity": "1"}],
                "tendered_cents": 18000,
                "buyer_pin": "not-a-pin",
            },
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "bad_buyer_pin"

    def test_a_refused_pin_leaves_no_sale_behind(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """The document and the sale share a transaction, so a refusal at this
        stage takes the sale with it rather than leaving one un-invoiced."""
        client_cashier_a.post(
            CHECKOUT,
            {
                "lines": [{"item_id": str(item_a.id), "quantity": "1"}],
                "tendered_cents": 18000,
                "buyer_pin": "not-a-pin",
            },
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 0
            assert ComplianceDocument.objects.count() == 0

    def test_an_unsettled_sale_cannot_be_invoiced(
        self, tenant_a, registered_tenant, store_a, cashier_a, item_a, stock_a
    ):
        """An invoice for money that has not been taken declares revenue the
        shop does not have, and a void afterwards would leave it standing."""
        from apps.sales.services import LineRequest, create_sale

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            assert sale.state == SaleState.OPEN

            with pytest.raises(ComplianceError) as exc:
                issue_invoice(sale=sale)

        assert exc.value.code == "sale_not_settled"

    def test_invoicing_the_same_sale_twice_returns_the_first_document(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """A retried checkout must not raise a second invoice, which would put
        a duplicate declaration into the series."""
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=settled["id"])
            first = ComplianceDocument.objects.get(sale=sale)
            again = issue_invoice(sale=sale)

            assert again.id == first.id
            assert ComplianceDocument.objects.filter(sale=sale).count() == 1


@pytest.mark.django_db
class TestBusinessesNotRegistered:
    def test_no_document_is_raised(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """Most small dukas are in exactly this position. Numbering an invoice
        would put entries in a series the shop does not have."""
        settled = sell(client_cashier_a, item_a)

        assert settled["state"] == SaleState.PAID
        with tenant_context(cashier_a.tenant_id):
            assert ComplianceDocument.objects.count() == 0

    def test_the_sale_still_settles_normally(
        self, client_cashier_a, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a)

        assert settled["receipt_number"] == 1
        assert settled["state"] == SaleState.PAID

    def test_a_customer_asking_for_a_tax_invoice_still_gets_one(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """An unregistered shop with a buyer PIN in hand has a customer who
        needs a document. Recording it costs nothing and refusing it would
        lose the only trace that the request was made."""
        settled = sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        assert document.adapter == "null"
        assert document.submission_state == SubmissionState.NOT_REQUIRED


@pytest.mark.django_db
class TestImmutability:
    def _document(self, client, cashier, item):
        settled = sell(client, item)
        with tenant_context(cashier.tenant_id):
            return ComplianceDocument.objects.get(sale_id=settled["id"])

    def test_a_blanket_save_is_refused(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        document = self._document(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document.gross_cents = 1
            with pytest.raises(ValueError, match="immutable"):
                document.save()

    def test_rewriting_a_figure_is_refused_even_by_update_fields(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        document = self._document(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document.gross_cents = 1
            with pytest.raises(ValueError, match="immutable"):
                document.save(update_fields=["gross_cents"])

    def test_rewriting_the_number_is_refused(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        document = self._document(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            with pytest.raises(ValueError, match="immutable"):
                document.save(update_fields=["invoice_number"])

    def test_rewriting_a_party_is_refused(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        document = self._document(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            with pytest.raises(ValueError, match="immutable"):
                document.save(update_fields=["buyer_pin"])

    def test_submission_bookkeeping_is_allowed_through(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """A submission that succeeds on the third attempt has not changed a
        single tax fact."""
        document = self._document(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document.submission_attempts += 1
            document.save(update_fields=["submission_attempts", "updated_at"])
            document.refresh_from_db()

        assert document.submission_attempts >= 1

    def test_the_message_says_what_to_do_instead(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        document = self._document(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            with pytest.raises(ValueError, match="credit note"):
                document.save()


@pytest.mark.django_db
class TestCreditNotes:
    def _invoice(self, client, cashier, item):
        settled = sell(client, item)
        with tenant_context(cashier.tenant_id):
            return ComplianceDocument.objects.get(sale_id=settled["id"])

    def test_a_credit_note_references_its_original(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            note = issue_credit_note(original=invoice, reason="Goods returned")

        assert note.kind == DocumentKind.CREDIT_NOTE
        assert note.original_id == invoice.id
        assert note.gross_cents == invoice.gross_cents

    def test_it_takes_its_own_number_in_the_same_series(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            note = issue_credit_note(original=invoice, reason="Goods returned")

        assert note.invoice_number == invoice.invoice_number + 1

    def test_the_original_is_untouched(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            issue_credit_note(original=invoice, reason="Goods returned")
            invoice.refresh_from_db()

        assert invoice.kind == DocumentKind.INVOICE
        assert invoice.gross_cents == 18000

    def test_a_reason_is_required(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            with pytest.raises(ComplianceError) as exc:
                issue_credit_note(original=invoice, reason="   ")

        assert exc.value.code == "reason_required"

    def test_an_invoice_cannot_be_credited_twice(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            issue_credit_note(original=invoice, reason="Goods returned")
            with pytest.raises(ComplianceError) as exc:
                issue_credit_note(original=invoice, reason="Again")

        assert exc.value.code == "already_credited"

    def test_a_credit_note_cannot_itself_be_credited(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            note = issue_credit_note(original=invoice, reason="Goods returned")
            with pytest.raises(ComplianceError) as exc:
                issue_credit_note(original=note, reason="Nonsense")

        assert exc.value.code == "already_a_credit_note"

    def test_the_figures_are_stored_positive(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        """The kind carries the direction. A negative figure on a document
        already called a credit note invites somebody to subtract it twice."""
        invoice = self._invoice(client_cashier_a, cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            note = issue_credit_note(original=invoice, reason="Goods returned")

        assert note.gross_cents > 0


@pytest.mark.django_db
class TestSubmission:
    def test_a_manual_business_queues_its_documents(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        settled = sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=settled["id"])

        # Queued, not sent inline. A checkout must never wait on a compliance
        # step - the goods have left the shop.
        assert document.submission_state == SubmissionState.PENDING

    def test_running_the_job_submits_them(
        self, client_cashier_a, cashier_a, registered_tenant, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(cashier_a.tenant_id):
            report = submit_pending(tenant=registered_tenant)
            document = ComplianceDocument.objects.first()

        assert report["submitted"] == 1
        assert document.submission_state == SubmissionState.SUBMITTED
        assert document.submitted_at is not None

    def test_a_business_with_no_tax_pin_fails_rather_than_pretending(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        """A tax invoice from an unidentified seller is not a tax invoice."""
        with tenant_context(tenant_a.id):
            tenant_a.compliance_mode = ComplianceMode.MANUAL
            tenant_a.kra_pin = ""
            tenant_a.save()

        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            report = submit_pending(tenant=tenant_a)
            document = ComplianceDocument.objects.first()

        assert report["failed"] == 1
        assert document.submission_state == SubmissionState.FAILED
        assert "tax PIN" in document.failure_detail

    def test_a_failure_is_retried_next_time(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        with tenant_context(tenant_a.id):
            tenant_a.compliance_mode = ComplianceMode.MANUAL
            tenant_a.kra_pin = ""
            tenant_a.save()
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            submit_pending(tenant=tenant_a)
            tenant_a.kra_pin = "P051234567X"
            tenant_a.save()
            # The seller PIN is frozen on the document, so fixing the setting
            # does not fix documents already raised - which is correct, and
            # worth pinning so nobody assumes otherwise.
            report = submit_pending(tenant=tenant_a)

        assert report["attempted"] == 1

    def test_an_unregistered_business_submits_nothing(
        self, client_cashier_a, tenant_a, item_a, stock_a
    ):
        sell(client_cashier_a, item_a, buyer_pin="P012345678Z")

        with tenant_context(tenant_a.id):
            report = submit_pending(tenant=tenant_a)

        assert report["attempted"] == 0

    def test_attempts_are_counted(
        self, client_cashier_a, cashier_a, tenant_a, item_a, stock_a
    ):
        with tenant_context(tenant_a.id):
            tenant_a.compliance_mode = ComplianceMode.MANUAL
            tenant_a.kra_pin = ""
            tenant_a.save()
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_a.id):
            submit_pending(tenant=tenant_a)
            submit_pending(tenant=tenant_a)
            document = ComplianceDocument.objects.first()

        assert document.submission_attempts == 2


@pytest.mark.django_db
class TestTheBreakdown:
    def test_lines_at_different_rates_are_grouped(
        self, tenant_a, store_a, cashier_a, item_a, exclusive_rate_a, stock_a, tax_rate_a
    ):
        from apps.catalog.models import Item
        from apps.sales.services import LineRequest, create_sale

        with tenant_context(tenant_a.id):
            zero_rated = Item.objects.create(
                tenant=tenant_a,
                sku="BREAD",
                name="Bread",
                price_cents=6000,
                tax_rate=None,
            )
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[
                    LineRequest(item_id=str(item_a.id), quantity=1),
                    LineRequest(item_id=str(zero_rated.id), quantity=1),
                ],
            )
            breakdown = tax_breakdown_for(sale)

        rates = [bucket["rate_bps"] for bucket in breakdown]
        assert rates == [1600, 0]
        assert breakdown[1]["tax_cents"] == 0

    def test_two_lines_at_one_rate_are_one_bucket(
        self, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        from apps.sales.services import LineRequest, create_sale

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[
                    LineRequest(item_id=str(item_a.id), quantity=1),
                    LineRequest(item_id=str(item_a.id), quantity=2),
                ],
            )
            breakdown = tax_breakdown_for(sale)

        assert len(breakdown) == 1
        assert breakdown[0]["gross_cents"] == 54000


@pytest.mark.django_db
class TestOfflineSalesHaveNoInvoiceUntilTheyLand:
    """Allocating a number on a disconnected till would make gaplessness across
    two tills unenforceable. So an offline sale has no invoice number until it
    syncs - which is when it commits.
    """

    def test_a_synced_sale_is_invoiced_on_arrival(
        self, client_cashier_a, cashier_a, registered_tenant, device_a, item_a, stock_a
    ):
        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        device, _token = device_a
        result = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a)]), format="json"
        ).json()["results"][0]

        with tenant_context(cashier_a.tenant_id):
            document = ComplianceDocument.objects.get(sale_id=result["sale_id"])

        assert document.invoice_number == 1
        assert document.issued_at is not None

    def test_the_number_comes_from_the_server_not_the_device(
        self, client_cashier_a, cashier_a, registered_tenant, device_a, item_a, stock_a
    ):
        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        device, _token = device_a
        payload = offline_sale(item_a)
        # A till cannot name an invoice number; there is no field for one.
        assert "invoice_number" not in payload

        result = client_cashier_a.post(
            SYNC, batch(device, [payload]), format="json"
        ).json()["results"][0]

        with tenant_context(cashier_a.tenant_id):
            assert ComplianceDocument.objects.get(sale_id=result["sale_id"]).invoice_number == 1

    def test_a_replayed_sale_does_not_take_a_second_number(
        self, client_cashier_a, cashier_a, registered_tenant, device_a, item_a, stock_a
    ):
        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        device, _token = device_a
        payload = offline_sale(item_a)
        body = batch(device, [payload])

        client_cashier_a.post(SYNC, body, format="json")
        client_cashier_a.post(SYNC, body, format="json")

        with tenant_context(cashier_a.tenant_id):
            assert ComplianceDocument.objects.count() == 1

    def test_a_backlog_is_numbered_in_the_order_it_synced(
        self, client_cashier_a, cashier_a, registered_tenant, device_a, item_a, stock_a
    ):
        from apps.sync.tests.test_sale_sync import SYNC, batch, offline_sale

        device, _token = device_a
        client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a), offline_sale(item_a)]),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            numbers = sorted(
                ComplianceDocument.objects.values_list("invoice_number", flat=True)
            )

        assert numbers == [1, 2]


@pytest.mark.django_db
class TestDocumentsStayInsideOneBusiness:
    def test_a_document_is_invisible_to_another_business(
        self, client_cashier_a, cashier_a, registered_tenant, tenant_b, item_a, stock_a
    ):
        sell(client_cashier_a, item_a)

        with tenant_context(tenant_b.id):
            assert ComplianceDocument.objects.count() == 0

    def test_the_invoice_counter_is_invisible_too(
        self, tenant_a, tenant_b
    ):
        from apps.compliance.models import InvoiceCounter

        with tenant_context(tenant_a.id):
            allocate_invoice_number(tenant_a)

        with tenant_context(tenant_b.id):
            assert InvoiceCounter.objects.count() == 0

    def test_one_businesss_numbers_do_not_advance_anothers(
        self, tenant_a, tenant_b
    ):
        with tenant_context(tenant_a.id):
            for _ in range(3):
                allocate_invoice_number(tenant_a)

        with tenant_context(tenant_b.id):
            number, _code = allocate_invoice_number(tenant_b)

        assert number == 1


@pytest.mark.django_db
class TestTheHook:
    def test_it_does_nothing_for_an_unregistered_shop(
        self, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        from apps.sales.services import LineRequest, create_sale, take_cash

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            take_cash(sale=sale, tendered_cents=18000, user=cashier_a)
            sale.refresh_from_db()

            assert issue_for_settled_sale(sale=sale) is None

    def test_doing_nothing_is_correct_rather_than_degraded(
        self, tenant_a, store_a, cashier_a, item_a, stock_a
    ):
        """Most small dukas are not registered. This is the common case."""
        from apps.sales.services import LineRequest, create_sale, take_cash

        with tenant_context(tenant_a.id):
            sale = create_sale(
                tenant=tenant_a,
                store=store_a,
                cashier=cashier_a,
                lines=[LineRequest(item_id=str(item_a.id), quantity=1)],
            )
            take_cash(sale=sale, tendered_cents=18000, user=cashier_a)
            sale.refresh_from_db()
            issue_for_settled_sale(sale=sale)

            assert sale.state == SaleState.PAID
            assert ComplianceDocument.objects.count() == 0
