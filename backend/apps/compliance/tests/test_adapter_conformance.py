"""
What every compliance adapter must do, whichever one it is.

Run against **both** implementations, from one suite. This is what makes the
boundary real rather than described: a third adapter cannot ship having quietly
redefined what ``issue`` means, because it has to pass these.

Kept apart from the adapters' own tests on purpose. What is asserted here is the
contract; what an adapter does *beyond* the contract is its own business and is
tested next door.
"""

from __future__ import annotations

import pytest

from apps.compliance.adapters import (
    ComplianceAdapter,
    ComplianceResult,
    ManualExportAdapter,
    NullAdapter,
    adapter_for,
)
from apps.compliance.models import DocumentKind, SubmissionState


class FakeDocument:
    """The shape an adapter is entitled to rely on.

    A stand-in rather than a database row, so the contract is tested without
    tying it to how a document happens to be stored. An adapter that reached
    for something not listed here would fail against this and should.
    """

    def __init__(self, *, kind=DocumentKind.INVOICE, seller_pin="P051234567X"):
        self.kind = kind
        self.invoice_code = "TI-2026-000001"
        self.invoice_number = 1
        self.seller_pin = seller_pin
        self.buyer_pin = ""
        self.net_cents = 15517
        self.tax_cents = 2483
        self.gross_cents = 18000
        self.tax_breakdown = [
            {"rate_bps": 1600, "net_cents": 15517, "tax_cents": 2483, "gross_cents": 18000}
        ]
        self.original = None
        self.submission_state = SubmissionState.PENDING
        self.submission_reference = ""

    @property
    def is_credit_note(self) -> bool:
        return self.kind == DocumentKind.CREDIT_NOTE


#: Every adapter in the system. A new one is added here and must pass unchanged.
ADAPTERS = [NullAdapter(), ManualExportAdapter()]
IDS = [adapter.name for adapter in ADAPTERS]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
class TestEveryAdapter:
    def test_it_satisfies_the_protocol(self, adapter):
        assert isinstance(adapter, ComplianceAdapter)

    def test_it_names_itself(self, adapter):
        """Recorded on each document, so a change of regime stays legible
        rather than making old rows look like they went somewhere else."""
        assert adapter.name
        assert isinstance(adapter.name, str)

    def test_it_declares_whether_it_submits(self, adapter):
        assert isinstance(adapter.submits, bool)

    def test_issuing_returns_a_result(self, adapter):
        result = adapter.issue(FakeDocument())

        assert isinstance(result, ComplianceResult)
        assert result.state in SubmissionState.values

    def test_crediting_returns_a_result(self, adapter):
        result = adapter.credit(FakeDocument(kind=DocumentKind.CREDIT_NOTE))

        assert isinstance(result, ComplianceResult)
        assert result.state in SubmissionState.values

    def test_status_returns_a_result(self, adapter):
        result = adapter.status(FakeDocument())

        assert isinstance(result, ComplianceResult)

    def test_it_never_raises_on_a_well_formed_document(self, adapter):
        """A failure is a result, not an exception.

        The far end is a government service over a Kenyan internet connection.
        An adapter that threw would take a sale down with it, and the goods
        have already left the shop.
        """
        document = FakeDocument()
        adapter.issue(document)
        adapter.credit(document)
        adapter.status(document)

    def test_it_never_raises_on_a_document_missing_a_seller_pin(self, adapter):
        """The commonest real defect, and still not an exception."""
        result = adapter.issue(FakeDocument(seller_pin=""))

        assert isinstance(result, ComplianceResult)

    def test_a_failure_carries_something_a_person_can_read(self, adapter):
        result = adapter.issue(FakeDocument(seller_pin=""))

        if result.state == SubmissionState.FAILED:
            assert result.detail, "a failure with no detail cannot be acted on"

    def test_success_and_retry_are_mutually_exclusive(self, adapter):
        for document in (
            FakeDocument(),
            FakeDocument(seller_pin=""),
            FakeDocument(kind=DocumentKind.CREDIT_NOTE),
        ):
            result = adapter.issue(document)
            assert not (result.succeeded and result.should_retry)

    def test_it_is_stateless_between_documents(self, adapter):
        """Two documents through the same adapter must not affect each other.

        An adapter that accumulated state would make a batch submission depend
        on the order it happened to run in.
        """
        first = adapter.issue(FakeDocument())
        adapter.issue(FakeDocument(seller_pin=""))
        again = adapter.issue(FakeDocument())

        assert again.state == first.state
        assert again.reference == first.reference


@pytest.mark.django_db
class TestChoosingAnAdapter:
    def test_a_business_under_no_regime_gets_the_null_adapter(self):
        assert adapter_for("NONE").name == "null"

    def test_a_manual_business_gets_the_export_adapter(self):
        assert adapter_for("MANUAL").name == "manual-export"

    def test_an_unknown_mode_falls_back_rather_than_raising(self):
        """A setting that has drifted - a regime withdrawn, a value
        hand-edited - must not stop a shop selling. It should stop it
        submitting, which is what the null adapter does."""
        adapter = adapter_for("ETIMS_FROM_THE_FUTURE")

        assert adapter.name == "null"
        assert adapter.submits is False
