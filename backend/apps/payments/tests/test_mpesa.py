"""
STK push and the callback that answers it.

The happy path is one test. Everything else here is a way the money could go
wrong: a callback replayed, a callback for a sale that has moved on, a forged
source address, a second prompt sent while the first is still live, a customer
who pays twice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from apps.accounts.constants import UserRole
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import bypass_rls, tenant_context
from apps.payments.models import (
    CallbackOutcome,
    IntentState,
    MpesaCallback,
    PaymentIntent,
    SuspectReason,
)
from apps.payments.testing import failure_callback, success_callback
from apps.sales.models import Payment, Sale, SaleDiscrepancy
from apps.sales.states import SaleState

pytestmark = pytest.mark.django_db

MPESA_CHECKOUT = "/api/v1/sales/checkout/mpesa/"


def callback_url(token: str) -> str:
    return f"/api/v1/payments/mpesa/callback/{token}/"


def start_sale(client, item, **overrides):
    payload = {
        "lines": [{"item_id": str(item.id), "quantity": "1"}],
        "phone": "0712345678",
    }
    payload.update(overrides)
    return client.post(MPESA_CHECKOUT, payload, format="json")


@pytest.fixture
def pending_intent(client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja, tenant_a):
    """A sale awaiting payment, with a live prompt sent."""
    response = start_sale(client_cashier_a, item_a)
    assert response.status_code == 202, response.content

    with transaction.atomic(), tenant_context(tenant_a.id):
        return PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])


class TestInitiation:
    def test_a_push_leaves_the_sale_awaiting_payment(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja
    ):
        response = start_sale(client_cashier_a, item_a)

        assert response.status_code == 202
        body = response.json()
        assert body["state"] == SaleState.AWAITING_PAYMENT
        assert body["receipt_number"] is None  # allocated when the money lands
        assert body["payment_intent"]["state"] == IntentState.PENDING

    def test_the_callback_url_carries_this_intent_s_token(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja, tenant_a
    ):
        response = start_sale(client_cashier_a, item_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])

        assert fake_daraja.pushes[0]["callback_url"].endswith(f"/{intent.callback_token}/")
        assert len(intent.callback_token) >= 32

    def test_the_phone_number_is_normalised_however_it_was_typed(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja
    ):
        """A cashier should not have to reformat while a customer waits."""
        for typed in ("0712345678", "+254712345678", "254712345678", "712345678"):
            fake_daraja.pushes.clear()
            start_sale(client_cashier_a, item_a, phone=typed)
            assert fake_daraja.pushes[0]["phone"] == typed

    def test_a_second_push_while_one_is_pending_is_refused(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja, pending_intent
    ):
        """Named test, not folded into a category.

        Two prompts answered by an obliging customer are two real payments, and
        nothing downstream can un-take money that was actually sent. This
        refusal is what keeps a genuine double charge rare rather than merely
        survivable.
        """
        with transaction.atomic(), tenant_context(pending_intent.tenant_id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)

        from apps.payments.services import StkError, initiate_stk

        with transaction.atomic(), tenant_context(pending_intent.tenant_id):
            with pytest.raises(StkError) as raised:
                initiate_stk(sale=sale, phone="0712345678", user=sale.cashier)

        assert raised.value.code == "push_already_pending"
        assert raised.value.status == 409

    def test_the_till_retrying_gets_the_same_intent_not_a_second_prompt(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja
    ):
        """Idempotency key one: the till's own retry after a timeout."""
        payment_uuid = "11111111-1111-1111-1111-111111111111"

        first = start_sale(client_cashier_a, item_a, payment_client_uuid=payment_uuid)
        pushes_after_first = len(fake_daraja.pushes)

        second = start_sale(client_cashier_a, item_a, payment_client_uuid=payment_uuid)

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["payment_intent"]["id"] == second.json()["payment_intent"]["id"]
        assert len(fake_daraja.pushes) == pushes_after_first

    def test_a_business_without_credentials_is_told_so(
        self, client_cashier_a, item_a, stock_a, fake_daraja
    ):
        response = start_sale(client_cashier_a, item_a)
        assert response.status_code == 400
        assert response.json()["code"] == "mpesa_not_configured"

    def test_daraja_refusing_leaves_no_sale_awaiting_payment(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja, tenant_a
    ):
        fake_daraja.should_fail = True
        response = start_sale(client_cashier_a, item_a)

        assert response.status_code == 400
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert not Sale.objects.filter(state=SaleState.AWAITING_PAYMENT).exists()

    def test_a_discount_needs_the_same_authority_as_on_the_cash_route(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja
    ):
        """The gate must not be enforced on one path and quietly not the other."""
        response = start_sale(client_cashier_a, item_a, cart_discount_cents=1000)

        assert response.status_code == 403
        assert response.json()["code"] == "discount_authorization_required"


class TestSuccessfulCallback:
    def test_the_money_lands_and_the_sale_is_paid(self, pending_intent, anon_client, tenant_a):
        response = anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(
                checkout_request_id=pending_intent.checkout_request_id,
                amount_shillings=180.0,
            ),
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["ResultCode"] == 0

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)
            payment = Payment.objects.get(sale=sale)

        assert sale.state == SaleState.PAID
        assert sale.receipt_number == 1  # allocated now, not at initiation
        assert payment.amount_cents == 18000
        assert payment.mpesa_receipt_number == "QK12ABC34D"

    def test_stock_moves_when_the_money_lands(self, pending_intent, anon_client, tenant_a, stock_a):
        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("39.000")

    def test_the_callback_is_recorded_as_applied(self, pending_intent, anon_client, tenant_a):
        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()
        assert record.outcome == CallbackOutcome.APPLIED
        assert record.raw_payload  # kept for disputes and for replaying in tests


class TestIdempotency:
    def test_safaricom_retrying_credits_once(self, pending_intent, anon_client, tenant_a):
        """Idempotency key three, and Safaricom does this routinely."""
        payload = success_callback(checkout_request_id=pending_intent.checkout_request_id)
        url = callback_url(pending_intent.callback_token)

        first = anon_client.post(url, payload, format="json")
        second = anon_client.post(url, payload, format="json")
        third = anon_client.post(url, payload, format="json")

        # A duplicate must look exactly like the first time, not like an error.
        assert first.status_code == second.status_code == third.status_code == 200
        assert second.json() == first.json()

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Payment.objects.count() == 1
            sale = Sale.objects.get(pk=pending_intent.sale_id)
        assert sale.total_cents == 18000

    def test_a_replay_does_not_move_stock_twice(
        self, pending_intent, anon_client, tenant_a, stock_a
    ):
        payload = success_callback(checkout_request_id=pending_intent.checkout_request_id)
        url = callback_url(pending_intent.callback_token)

        anon_client.post(url, payload, format="json")
        anon_client.post(url, payload, format="json")

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("39.000")

    def test_the_same_receipt_number_cannot_be_credited_twice(
        self, pending_intent, anon_client, tenant_a, item_a, stock_a,
        client_cashier_a, sandbox_credential, fake_daraja,
    ):
        """Idempotency key four: the last line of defence.

        Even reached by a different intent, one real movement of money cannot be
        applied to a business twice.
        """
        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        fake_daraja.checkout_request_id = "ws_CO_SECOND"
        second = start_sale(client_cashier_a, item_a)
        with transaction.atomic(), tenant_context(tenant_a.id):
            other = PaymentIntent.objects.get(pk=second.json()["payment_intent"]["id"])

        anon_client.post(
            callback_url(other.callback_token),
            success_callback(
                checkout_request_id="ws_CO_SECOND", receipt="QK12ABC34D"
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Payment.objects.filter(mpesa_receipt_number="QK12ABC34D").count() == 1


class TestTerminalStateGuard:
    def test_a_callback_for_a_voided_sale_leaves_it_void(
        self, pending_intent, anon_client, tenant_a, manager_a
    ):
        from apps.sales.services import void_sale

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)
            void_sale(sale=sale, user=manager_a, reason="Customer walked away")

        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale.refresh_from_db()
            record = MpesaCallback.objects.get()

        assert sale.state == SaleState.VOID
        assert record.outcome == CallbackOutcome.SUSPECT
        assert record.suspect_reason == SuspectReason.SALE_NOT_AWAITING
        assert not Payment.objects.filter(sale=sale).exists()

    def test_the_guard_reads_the_ledger_not_the_cached_state(
        self, pending_intent, anon_client, tenant_a, cashier_a
    ):
        """Force the cache to lie, then deliver a valid callback.

        The sale's column says it is still awaiting payment. The ledger says it
        was already settled in cash. The money is what decides, so the callback
        is held rather than applied - which is the same discipline the void
        guard uses, and for the same reason: a guard that only holds while the
        cache is correct is no guard at all.
        """
        from apps.sales.services import take_cash

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)
            take_cash(sale=sale, tendered_cents=20000, user=cashier_a)
            sale.refresh_from_db()
            assert sale.state == SaleState.PAID

            # Straight to the database, behind the service's back.
            Sale.objects.filter(pk=sale.pk).update(state=SaleState.AWAITING_PAYMENT)

        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()
            payments = list(Payment.objects.filter(sale_id=pending_intent.sale_id))

        assert record.outcome == CallbackOutcome.SUSPECT
        assert record.suspect_reason == SuspectReason.SALE_NOT_AWAITING
        assert len(payments) == 1  # the cash one, not a second from M-Pesa
        assert payments[0].method == "CASH"

    def test_a_held_callback_raises_a_discrepancy_for_a_person(
        self, pending_intent, anon_client, tenant_a, manager_a
    ):
        """Money the shop is holding but has not applied must be visible."""
        from apps.sales.services import void_sale

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)
            void_sale(sale=sale, user=manager_a, reason="Customer walked away")

        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.LATE_PAYMENT
            )
        assert discrepancy.is_open
        assert "not been applied" in discrepancy.detail

    def test_a_mismatched_request_id_is_held(self, pending_intent, anon_client, tenant_a):
        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id="ws_CO_SOMETHING_ELSE"),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()
        assert record.suspect_reason == SuspectReason.REQUEST_ID_MISMATCH

    def test_a_mismatched_amount_is_held(self, pending_intent, anon_client, tenant_a):
        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(
                checkout_request_id=pending_intent.checkout_request_id,
                amount_shillings=5.0,
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()
            assert not Payment.objects.exists()
        assert record.suspect_reason == SuspectReason.AMOUNT_MISMATCH


class TestUnknownToken:
    def test_an_unknown_token_is_acknowledged_not_errored(self, anon_client):
        """Named test, not folded into a category.

        Anything but a 200 makes Safaricom retry, and a retry of a callback we
        deliberately refused only buries the ones worth looking at.
        """
        response = anon_client.post(
            callback_url("a-token-that-matches-nothing"),
            success_callback(),
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["ResultCode"] == 0

    def test_an_unknown_token_is_still_recorded(self, anon_client):
        """With no business, since there is none to attribute it to.

        Visible only from the platform surface: the isolation policy matches
        nothing when the tenant is null.
        """
        anon_client.post(
            callback_url("a-token-that-matches-nothing"), success_callback(), format="json"
        )

        with transaction.atomic(), bypass_rls():
            record = MpesaCallback.all_objects.get()

        assert record.tenant_id is None
        assert record.intent_id is None
        assert record.outcome == CallbackOutcome.SUSPECT
        assert record.suspect_reason == SuspectReason.UNKNOWN_TOKEN

    def test_an_unknown_token_creates_no_payment(self, anon_client):
        anon_client.post(
            callback_url("a-token-that-matches-nothing"), success_callback(), format="json"
        )
        with transaction.atomic(), bypass_rls():
            assert Payment.all_objects.count() == 0


class TestIpAllowlist:
    def test_sandbox_ignores_the_allowlist(
        self, pending_intent, anon_client, tenant_a, sandbox_credential
    ):
        """A business integrating from an unpredictable address is not blocked."""
        response = anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
            REMOTE_ADDR="203.0.113.77",
        )
        assert response.status_code == 200

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert MpesaCallback.objects.get().outcome == CallbackOutcome.APPLIED

    def test_production_refuses_an_address_not_on_the_list(
        self, client_cashier_a, item_a, stock_a, production_credential, fake_daraja,
        anon_client, tenant_a, settings,
    ):
        settings.TRUSTED_PROXY_HOPS = 1
        response = start_sale(client_cashier_a, item_a)
        with transaction.atomic(), tenant_context(tenant_a.id):
            intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])

        anon_client.post(
            callback_url(intent.callback_token),
            success_callback(checkout_request_id=intent.checkout_request_id),
            format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.77",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()
            assert not Payment.objects.exists()
        assert record.suspect_reason == SuspectReason.UNTRUSTED_SOURCE

    def test_production_accepts_an_address_on_the_list(
        self, client_cashier_a, item_a, stock_a, production_credential, fake_daraja,
        anon_client, tenant_a, settings,
    ):
        settings.TRUSTED_PROXY_HOPS = 1
        response = start_sale(client_cashier_a, item_a)
        with transaction.atomic(), tenant_context(tenant_a.id):
            intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])

        anon_client.post(
            callback_url(intent.callback_token),
            success_callback(checkout_request_id=intent.checkout_request_id),
            format="json",
            HTTP_X_FORWARDED_FOR="196.201.214.200",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert MpesaCallback.objects.get().outcome == CallbackOutcome.APPLIED

    def test_a_spoofed_leading_entry_does_not_get_past(
        self, client_cashier_a, item_a, stock_a, production_credential, fake_daraja,
        anon_client, tenant_a, settings,
    ):
        """The reason the LAST entry is the one read.

        A caller sets X-Forwarded-For themselves; our proxy then appends what it
        actually saw. Reading the first entry - the common default - would let
        anyone claim to be Safaricom by typing their address into a header.
        """
        settings.TRUSTED_PROXY_HOPS = 1
        response = start_sale(client_cashier_a, item_a)
        with transaction.atomic(), tenant_context(tenant_a.id):
            intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])

        anon_client.post(
            callback_url(intent.callback_token),
            success_callback(checkout_request_id=intent.checkout_request_id),
            format="json",
            # The attacker's claim first, what our proxy saw last.
            HTTP_X_FORWARDED_FOR="196.201.214.200, 203.0.113.77",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            record = MpesaCallback.objects.get()
            assert not Payment.objects.exists()
        assert record.suspect_reason == SuspectReason.UNTRUSTED_SOURCE

    def test_production_with_an_empty_allowlist_fails_closed(
        self, client_cashier_a, item_a, stock_a, production_credential, fake_daraja,
        anon_client, tenant_a, settings,
    ):
        """Not configured must not look the same as configured correctly."""
        settings.TRUSTED_PROXY_HOPS = 1
        with transaction.atomic(), tenant_context(tenant_a.id):
            production_credential.allowed_callback_ips = []
            production_credential.save(update_fields=["allowed_callback_ips"])

        response = start_sale(client_cashier_a, item_a)
        with transaction.atomic(), tenant_context(tenant_a.id):
            intent = PaymentIntent.objects.get(pk=response.json()["payment_intent"]["id"])

        anon_client.post(
            callback_url(intent.callback_token),
            success_callback(checkout_request_id=intent.checkout_request_id),
            format="json",
            HTTP_X_FORWARDED_FOR="196.201.214.200",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert MpesaCallback.objects.get().suspect_reason == SuspectReason.UNTRUSTED_SOURCE


class TestFailedPayment:
    def test_a_declined_prompt_returns_the_sale_to_the_cart(
        self, pending_intent, anon_client, tenant_a
    ):
        """So the cashier can retry or take cash, without re-ringing."""
        anon_client.post(
            callback_url(pending_intent.callback_token),
            failure_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)
            record = MpesaCallback.objects.get()
            pending_intent.refresh_from_db()

        assert sale.state == SaleState.OPEN
        assert pending_intent.state == IntentState.FAILED
        assert record.outcome == CallbackOutcome.FAILED_PAYMENT

    def test_a_failure_callback_without_metadata_is_handled(
        self, pending_intent, anon_client, tenant_a
    ):
        """Safaricom omits the metadata block entirely on a failure."""
        response = anon_client.post(
            callback_url(pending_intent.callback_token),
            failure_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )
        assert response.status_code == 200


class TestOverpayment:
    def test_a_second_real_payment_is_recorded_not_refused(
        self, pending_intent, anon_client, tenant_a, cashier_a
    ):
        """Two genuine receipts on one sale.

        The money is real. Refusing to record it would put the books further
        from the truth than an overpayment flag does.
        """

        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            sale = Sale.objects.get(pk=pending_intent.sale_id)
            Payment.objects.create(
                tenant=sale.tenant,
                sale=sale,
                method="MPESA",
                amount_cents=18000,
                mpesa_receipt_number="QK99ZZZ99Z",
                user=cashier_a,
            )
            from apps.sales.services import recompute_state

            recompute_state(sale)
            sale.refresh_from_db()

        assert sale.is_overpaid is True
        assert sale.state == SaleState.PAID


class TestCallbackIsolation:
    def test_a_callback_only_ever_touches_its_own_business(
        self, pending_intent, anon_client, tenant_a, tenant_b
    ):
        anon_client.post(
            callback_url(pending_intent.callback_token),
            success_callback(checkout_request_id=pending_intent.checkout_request_id),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_b.id):
            assert Payment.all_objects.count() == 0
            assert MpesaCallback.all_objects.count() == 0

    def test_the_bypass_is_only_used_to_resolve_the_token(self):
        """Structural: exactly one route may run before a tenant is bound.

        The callback is the third and last place bypass_rls is reached, after
        the platform surfaces. Any new route under this prefix would be a fourth
        and needs the same argument made for it.
        """
        from apps.payments import urls as payment_urls

        paths = [str(pattern.pattern) for pattern in payment_urls.urlpatterns]
        assert paths == ["mpesa/callback/<str:token>/"]


@pytest.fixture
def manager_with_pin(tenant_a):
    """A manager who can approve a discount at the till."""
    from apps.accounts.models import User

    with transaction.atomic(), tenant_context(tenant_a.id):
        user = User(
            tenant=tenant_a,
            username="peter",
            full_name="Peter Omondi",
            role=UserRole.MANAGER,
        )
        user.set_password("peter-pass-8823")
        user.set_pin("4471")
        user.save()
        return user


class TestFailedAuthorizationIsAudited:
    """The same tripwire the cash path has, on the path that pays by phone.

    A discount is a discount however the customer pays, so the gate is the same
    call - and so is the failure mode it guards against. Authority is resolved
    **before** the view opens its transaction, and a refusal *returns* rather
    than raising, so the audit entry survives. Wrapping that check in an atomic
    block later would discard the entry on the way out, which is exactly what
    happened to the restaurant module's order void before it was fixed.

    These exist so that refactor fails a test instead of shipping.
    """

    def _refused_push(self, client, item):
        return start_sale(
            client,
            item,
            cart_discount_cents=1000,
            discount_authorization={
                "username": "peter",
                "pin": "0000",
                "reason": "Because",
            },
        )

    def test_a_wrong_pin_is_recorded(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja,
        manager_with_pin, tenant_a,
    ):
        self._refused_push(client_cashier_a, item_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).first()

        assert entry is not None
        assert entry.entity_id == "peter"
        assert entry.reason == "bad_credential"

    def test_the_entry_survives_the_refusal(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja,
        manager_with_pin, tenant_a,
    ):
        """The point of the whole thing.

        The push is refused and nothing is written - no sale, no intent - but
        the record that somebody tried must outlive the refusal, or a person
        working through a manager's four digits leaves no trace at all.
        """
        response = self._refused_push(client_cashier_a, item_a)

        assert response.status_code == 403
        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Sale.objects.count() == 0
            assert PaymentIntent.objects.count() == 0
            assert AuditLog.objects.filter(
                action=AuditAction.DISCOUNT_REFUSED
            ).exists()

    def test_the_entry_names_the_cashier_but_not_the_manager(
        self, client_cashier_a, cashier_a, item_a, stock_a, sandbox_credential,
        fake_daraja, manager_with_pin, tenant_a,
    ):
        """Filed against the username string, like a failed sign-in.

        Nobody proved they were that manager, so attaching them by foreign key
        would put someone else's guessing into their history.
        """
        self._refused_push(client_cashier_a, item_a)

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).first()

        assert entry.actor_id == cashier_a.id
        assert entry.after["acting_cashier"] == "mary"
        assert entry.after["attempted_authorizer"] == "peter"

    def test_the_pin_is_never_in_the_entry(
        self, client_cashier_a, item_a, stock_a, sandbox_credential, fake_daraja,
        manager_with_pin, tenant_a,
    ):
        start_sale(
            client_cashier_a,
            item_a,
            cart_discount_cents=1000,
            discount_authorization={
                "username": "peter",
                "pin": "9137",
                "reason": "Because",
            },
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).first()

        assert "9137" not in str(entry.after)
        assert "9137" not in str(entry.before)
