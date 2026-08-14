"""
Chasing payments whose callback never arrived.

The property that matters most is that this cannot credit twice - not against a
second run of itself, and not against a callback landing at the same moment.
"""

from __future__ import annotations

import pytest
from django.db import transaction
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.payments.models import IntentState, MpesaCallback, PaymentIntent
from apps.payments.reconciliation import reconcile
from apps.payments.services import RECONCILED_PREFIX
from apps.payments.testing import success_callback
from apps.sales.models import Payment, Sale
from apps.sales.states import SaleState

pytestmark = pytest.mark.django_db

MPESA_CHECKOUT = "/api/v1/sales/checkout/mpesa/"


@pytest.fixture
def lapsed_intent(client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja, tenant_a):
    """A prompt that was sent, expired, and never heard back about."""
    response = client_cashier_a.post(
        MPESA_CHECKOUT,
        {"lines": [{"item_id": str(item_a.id), "quantity": "1"}], "phone": "0712345678"},
        format="json",
    )
    assert response.status_code == 202, response.content

    with transaction.atomic(), tenant_context(tenant_a.id):
        intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])
        PaymentIntent.objects.filter(pk=intent.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=10)
        )
        intent.refresh_from_db()
        return intent


class TestFindingLapsedAttempts:
    def test_a_live_prompt_is_left_alone(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja
    ):
        """Chasing one while the customer is still typing asks Daraja a question
        it cannot yet answer."""
        client_cashier_a.post(
            MPESA_CHECKOUT,
            {"lines": [{"item_id": str(item_a.id), "quantity": "1"}], "phone": "0712345678"},
            format="json",
        )

        assert reconcile().examined == 0

    def test_a_lapsed_prompt_is_examined(self, lapsed_intent, fake_daraja):
        assert reconcile().examined == 1


class TestConfirmedPaid:
    def test_the_money_is_credited_and_the_sale_settles(
        self, lapsed_intent, fake_daraja, tenant_a
    ):
        fake_daraja.query_result_code = 0

        report = reconcile()

        assert report.credited == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=lapsed_intent.sale_id)
            payment = Payment.objects.get(sale=sale)
            lapsed_intent.refresh_from_db()

        assert sale.state == SaleState.PAID
        assert sale.receipt_number == 1
        assert payment.amount_cents == 18000
        assert lapsed_intent.state == IntentState.SUCCEEDED
        assert lapsed_intent.reconciled_at is not None

    def test_the_reference_says_where_it_came_from(
        self, lapsed_intent, fake_daraja, tenant_a
    ):
        """A status query does not return the M-Pesa receipt code.

        Crediting with a blank reference would disable the one constraint that
        catches a double credit by any path, so the checkout request id is used
        instead - unique per attempt, and visibly not an M-Pesa code.
        """
        fake_daraja.query_result_code = 0
        reconcile()

        with transaction.atomic(), tenant_context(tenant_a.id):
            payment = Payment.objects.get()

        assert payment.mpesa_receipt_number.startswith(RECONCILED_PREFIX)
        assert lapsed_intent.checkout_request_id in payment.mpesa_receipt_number

    def test_stock_moves_exactly_once(self, lapsed_intent, fake_daraja, tenant_a, stock_a):
        from decimal import Decimal

        fake_daraja.query_result_code = 0
        reconcile()
        reconcile()

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("39.000")


class TestCannotCreditTwice:
    def test_running_twice_credits_once(self, lapsed_intent, fake_daraja, tenant_a):
        fake_daraja.query_result_code = 0

        first = reconcile()
        second = reconcile()

        assert first.credited == 1
        assert second.examined == 0  # no longer pending, so not even looked at

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Payment.objects.count() == 1

    def test_a_callback_arriving_after_reconciliation_does_not_credit_again(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        """The race, in the order that actually happens.

        Reconciliation credits, then the missing callback finally turns up. It
        finds the intent already resolved, so no second payment is made - but it
        carries the real M-Pesa receipt code, which is backfilled onto the
        existing payment. See TestBackfillingTheRealReceiptCode.
        """
        fake_daraja.query_result_code = 0
        reconcile()

        response = anon_client.post(
            f"/api/v1/payments/mpesa/callback/{lapsed_intent.callback_token}/",
            success_callback(
                checkout_request_id=lapsed_intent.checkout_request_id,
                receipt="QK12ABC34D",
            ),
            format="json",
        )
        assert response.status_code == 200

        with transaction.atomic(), tenant_context(tenant_a.id):
            payments = list(Payment.objects.all())
            record = MpesaCallback.objects.get()

        assert len(payments) == 1  # credited once, not twice
        assert payments[0].mpesa_receipt_number == "QK12ABC34D"
        assert record.mpesa_receipt_number == "QK12ABC34D"

    def test_reconciliation_after_a_callback_does_nothing(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        """The same race the other way round."""
        anon_client.post(
            f"/api/v1/payments/mpesa/callback/{lapsed_intent.callback_token}/",
            success_callback(checkout_request_id=lapsed_intent.checkout_request_id),
            format="json",
        )

        fake_daraja.query_result_code = 0
        report = reconcile()

        assert report.examined == 0
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Payment.objects.count() == 1

    def test_it_uses_the_same_guard_as_the_callback(
        self, lapsed_intent, fake_daraja, tenant_a, manager_a
    ):
        """A voided sale is refused by reconciliation exactly as by a callback.

        Not a second implementation that happens to behave similarly - the same
        settle_intent, so the two cannot drift apart.
        """
        from apps.sales.services import void_sale

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=lapsed_intent.sale_id)
            void_sale(sale=sale, user=manager_a, reason="Customer left")

        fake_daraja.query_result_code = 0
        report = reconcile()

        assert report.credited == 0
        assert report.skipped == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale.refresh_from_db()
            assert sale.state == SaleState.VOID
            assert not Payment.objects.exists()


class TestConfirmedUnpaid:
    def test_a_cancelled_prompt_returns_the_sale_to_the_cart(
        self, lapsed_intent, fake_daraja, tenant_a
    ):
        fake_daraja.query_result_code = 1032
        fake_daraja.query_description = "Request cancelled by user"

        report = reconcile()

        assert report.failed == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=lapsed_intent.sale_id)
            lapsed_intent.refresh_from_db()

        assert sale.state == SaleState.OPEN
        assert lapsed_intent.state == IntentState.FAILED
        assert not Payment.objects.exists()


class TestWhenDarajaCannotAnswer:
    def test_a_still_processing_answer_leaves_it_pending(
        self, lapsed_intent, fake_daraja, tenant_a
    ):
        """Reading 'still processing' as a failure would tell a customer who
        paid that they had not."""
        fake_daraja.query_result_code = -1
        fake_daraja.query_raw = {"errorCode": "500.001.1001"}

        report = reconcile()

        assert report.still_pending == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            lapsed_intent.refresh_from_db()
        assert lapsed_intent.state == IntentState.PENDING

    def test_an_unreachable_daraja_changes_nothing(
        self, lapsed_intent, fake_daraja, tenant_a, monkeypatch
    ):
        """Not evidence of anything, so nothing is concluded from it."""
        from apps.payments.daraja import DarajaError

        def refuse(**kwargs):
            raise DarajaError("Could not reach M-Pesa.", "daraja_unreachable")

        monkeypatch.setattr(fake_daraja, "stk_query", refuse)

        report = reconcile()

        assert report.unreachable == 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            lapsed_intent.refresh_from_db()
        assert lapsed_intent.state == IntentState.PENDING


class TestTheCommand:
    def test_it_runs_and_reports(self, lapsed_intent, fake_daraja, capsys):
        from django.core.management import call_command

        fake_daraja.query_result_code = 0
        call_command("reconcile_mpesa")

        output = capsys.readouterr().out
        assert "Examined 1" in output
        assert "1 credited" in output

    def test_quiet_says_nothing_when_there_is_nothing_to_do(self, fake_daraja, capsys):
        from django.core.management import call_command

        call_command("reconcile_mpesa", quiet=True)
        assert capsys.readouterr().out == ""


class TestReconciliationIsolation:
    def test_it_only_touches_the_business_that_owns_the_intent(
        self, lapsed_intent, fake_daraja, tenant_a, tenant_b
    ):
        fake_daraja.query_result_code = 0
        reconcile()

        with transaction.atomic(), tenant_context(tenant_b.id):
            assert Payment.all_objects.count() == 0


class TestBackfillingTheRealReceiptCode:
    """The late callback that turns a placeholder into ground truth.

    A reconciled payment carries ``RECON-<checkout request id>`` because
    Daraja's status query does not return an M-Pesa receipt code. That keeps the
    unique constraint working but is useless for matching against the business's
    own M-Pesa statement - and if the missing callback ever turns up, it carries
    the real code. This is the only moment that code becomes available.
    """

    def _reconciled_then_callback(self, intent, anon_client, tenant, receipt="QK12ABC34D"):
        reconcile()
        response = anon_client.post(
            f"/api/v1/payments/mpesa/callback/{intent.callback_token}/",
            success_callback(
                checkout_request_id=intent.checkout_request_id, receipt=receipt
            ),
            format="json",
        )
        assert response.status_code == 200
        return response

    def test_the_placeholder_is_replaced_with_the_real_code(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        fake_daraja.query_result_code = 0

        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            payment = Payment.objects.get()

        assert payment.mpesa_receipt_number == "QK12ABC34D"
        assert not payment.mpesa_receipt_number.startswith(RECONCILED_PREFIX)

    def test_confirmed_via_flips_to_callback(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        from apps.sales.models import PaymentConfirmation

        fake_daraja.query_result_code = 0
        reconcile()

        with transaction.atomic(), tenant_context(tenant_a.id):
            before = Payment.objects.get()
        assert before.confirmed_via == PaymentConfirmation.RECONCILIATION

        anon_client.post(
            f"/api/v1/payments/mpesa/callback/{lapsed_intent.callback_token}/",
            success_callback(
                checkout_request_id=lapsed_intent.checkout_request_id,
                receipt="QK12ABC34D",
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            after = Payment.objects.get()
        assert after.confirmed_via == PaymentConfirmation.CALLBACK

    def test_the_money_is_untouched(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        """A placeholder is replaced by the fact it stood in for.

        Same amount, same sale, same intent, same timestamp - nothing about the
        money changes, which is what makes editing an otherwise append-only row
        acceptable here.
        """
        fake_daraja.query_result_code = 0
        reconcile()

        with transaction.atomic(), tenant_context(tenant_a.id):
            before = Payment.objects.get()
            before_amount, before_sale, before_created = (
                before.amount_cents,
                before.sale_id,
                before.created_at,
            )

        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            after = Payment.objects.get()
            payment_count = Payment.objects.count()

        assert after.amount_cents == before_amount
        assert after.sale_id == before_sale
        assert after.created_at == before_created
        assert payment_count == 1

    def test_the_sale_state_is_untouched(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        fake_daraja.query_result_code = 0
        reconcile()

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=lapsed_intent.sale_id)
            receipt_before = sale.receipt_number

        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale.refresh_from_db()

        assert sale.state == SaleState.PAID
        assert sale.receipt_number == receipt_before
        assert sale.is_overpaid is False

    def test_the_late_callback_is_still_recorded_as_suspect(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        """Useful now rather than merely noise - but still worth a person's eye.

        A payment arriving after settlement means the callback path was broken
        for a while, and that is worth knowing even though the backfill has made
        this particular one productive.
        """
        from apps.payments.models import CallbackOutcome, SuspectReason

        fake_daraja.query_result_code = 0
        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()

        assert record.outcome == CallbackOutcome.SUSPECT
        assert record.suspect_reason == SuspectReason.INTENT_ALREADY_SETTLED
        assert record.mpesa_receipt_number == "QK12ABC34D"

    def test_the_backfill_is_audited(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        """Editing a payment row is unusual enough to leave a trail."""
        from apps.core.models import AuditAction, AuditLog

        fake_daraja.query_result_code = 0
        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(
                action=AuditAction.UPDATE, entity_type="sales.Payment"
            ).first()

        assert entry is not None
        assert entry.before["confirmed_via"] == "RECONCILIATION"
        assert entry.after["mpesa_receipt_number"] == "QK12ABC34D"

    def test_a_second_late_callback_changes_nothing_further(
        self, lapsed_intent, fake_daraja, tenant_a, anon_client
    ):
        """Safaricom retries this one too."""
        fake_daraja.query_result_code = 0
        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)
        self._reconciled_then_callback(lapsed_intent, anon_client, tenant_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            payments = list(Payment.objects.all())

        assert len(payments) == 1
        assert payments[0].mpesa_receipt_number == "QK12ABC34D"

    def test_a_callback_settled_payment_is_never_backfilled_over(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja,
        tenant_a, anon_client,
    ):
        """Only a reconciliation placeholder is replaceable.

        A payment already confirmed by callback holds a real code, and
        overwriting it would be losing ground truth rather than gaining it.
        """
        from apps.sales.models import PaymentConfirmation

        response = client_cashier_a.post(
            MPESA_CHECKOUT,
            {"lines": [{"item_id": str(item_a.id), "quantity": "1"}], "phone": "0712345678"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])

        anon_client.post(
            f"/api/v1/payments/mpesa/callback/{intent.callback_token}/",
            success_callback(
                checkout_request_id=intent.checkout_request_id, receipt="QKFIRST001"
            ),
            format="json",
        )
        anon_client.post(
            f"/api/v1/payments/mpesa/callback/{intent.callback_token}/",
            success_callback(
                checkout_request_id=intent.checkout_request_id, receipt="QKSECOND02"
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            payment = Payment.objects.get()

        assert payment.mpesa_receipt_number == "QKFIRST001"
        assert payment.confirmed_via == PaymentConfirmation.CALLBACK
