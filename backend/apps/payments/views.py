"""
The M-Pesa callback.

The only unauthenticated route in this system that moves money, so it gets the
same scrutiny as the platform bypass - and for the same reason: it runs a query
before any tenant is bound.

**The bypass window is one statement.** A callback carries no tenant and no
credential, so the token in its URL is the only thing that can resolve it. That
lookup is a single ``SELECT ... WHERE callback_token = %s LIMIT 1`` with no
joins, no writes, and nothing else in the block. The moment an intent is found,
its tenant is bound and everything after is ordinary tenant-scoped code.

**Everything gets recorded.** A callback that is refused is stored with the
reason, because when a customer insists they paid, "we received this at 14:02
and refused it because the sale had been voided at 14:01" is a materially
different answer from having no record at all.

**Safaricom always gets a 200.** Anything else makes them retry, and a retry of
a callback we have deliberately refused is noise that buries the ones worth
looking at.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.net import client_ip, is_ip_allowed
from apps.core.tenancy import bypass_rls, tenant_context
from apps.payments.daraja import parse_callback
from apps.payments.models import (
    CallbackOutcome,
    MpesaCallback,
    MpesaCredential,
    PaymentIntent,
    SuspectReason,
)
from apps.payments.services import mark_intent_failed, settle_intent

logger = logging.getLogger(__name__)

#: Safaricom expects this shape and stops retrying when it sees it.
_ACKNOWLEDGED = {"ResultCode": 0, "ResultDesc": "Accepted"}


@extend_schema(tags=["payments"])
class MpesaCallbackView(APIView):
    """Where Safaricom posts the result of an STK push."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        summary="M-Pesa STK callback",
        description=(
            "Called by Safaricom, not by a client. The token in the path "
            "identifies which payment attempt this is about, which is how an "
            "unauthenticated request resolves to one business and one sale.\n\n"
            "Always returns 200. Anything else makes Safaricom retry, and a "
            "retry of a callback that was deliberately refused only buries the "
            "ones worth looking at. Whether the money was applied is recorded, "
            "not signalled in the status code."
        ),
        responses={200: OpenApiResponse(description="Acknowledged")},
    )
    def post(self, request, token: str):
        parsed = parse_callback(request.data if isinstance(request.data, dict) else {})
        source_ip = client_ip(request)

        # --- The bypass window. One row, by token, nothing else. -----------
        with transaction.atomic(), bypass_rls():
            intent = (
                PaymentIntent.all_objects.select_related("tenant")
                .filter(callback_token=token)
                .first()
            )

        if intent is None:
            # Nothing to bind a tenant from, so this is recorded without one.
            # Still recorded: an unknown token is either a scanner or a bug, and
            # both are worth being able to see.
            logger.warning("M-Pesa callback with an unrecognised token")
            self._record_tenantless(parsed, source_ip, request.data)
            return Response(_ACKNOWLEDGED, status=status.HTTP_200_OK)

        with transaction.atomic(), tenant_context(intent.tenant_id):
            return self._handle(intent, parsed, source_ip, request.data)

    # ------------------------------------------------------------------

    def _handle(self, intent, parsed, source_ip, raw):
        """Everything after the tenant is bound."""
        credential = MpesaCredential.objects.filter(tenant=intent.tenant).first()

        # Production callbacks must come from an address we know. Sandbox is
        # exempt, so a business integrating from behind an unpredictable
        # address is never blocked while no real money is moving.
        if credential is not None and credential.requires_ip_allowlist:
            if not is_ip_allowed(self.request, credential.allowed_callback_ips):
                return self._record(
                    intent,
                    parsed,
                    source_ip,
                    raw,
                    outcome=CallbackOutcome.SUSPECT,
                    suspect_reason=SuspectReason.UNTRUSTED_SOURCE,
                )

        # The request id must match the intent the token pointed at. A token
        # and a request id from different attempts means something is wrong
        # enough to want a person.
        if (
            parsed["checkout_request_id"]
            and intent.checkout_request_id
            and parsed["checkout_request_id"] != intent.checkout_request_id
        ):
            return self._record(
                intent,
                parsed,
                source_ip,
                raw,
                outcome=CallbackOutcome.SUSPECT,
                suspect_reason=SuspectReason.REQUEST_ID_MISMATCH,
            )

        # A customer who declined or let it time out. Not suspect: an ordinary
        # outcome, and the sale goes back to being a cart.
        if parsed["result_code"] not in (0, "0"):
            mark_intent_failed(
                intent=intent,
                result_code=int(parsed["result_code"] or -1),
                description=parsed["result_description"],
            )
            return self._record(
                intent, parsed, source_ip, raw, outcome=CallbackOutcome.FAILED_PAYMENT
            )

        outcome = settle_intent(
            intent=intent,
            mpesa_receipt_number=parsed["mpesa_receipt_number"],
            amount_cents=parsed["amount_cents"] or 0,
            phone=parsed["phone"],
            source="CALLBACK",
        )

        return self._record(
            intent,
            parsed,
            source_ip,
            raw,
            outcome=outcome.outcome,
            suspect_reason=outcome.suspect_reason,
        )

    def _record(self, intent, parsed, source_ip, raw, *, outcome, suspect_reason=""):
        """Store what arrived and what was done with it.

        The unique constraint on the checkout request id is idempotency key
        three: Safaricom retrying a callback cannot insert a second row, so it
        cannot cause a second credit. Hitting it is a duplicate, not an error,
        and the same acknowledgement goes back as the first time.
        """
        try:
            with transaction.atomic():
                MpesaCallback.objects.create(
                    tenant=intent.tenant,
                    intent=intent,
                    checkout_request_id=parsed["checkout_request_id"],
                    merchant_request_id=parsed["merchant_request_id"],
                    mpesa_receipt_number=parsed["mpesa_receipt_number"],
                    result_code=parsed["result_code"],
                    result_description=parsed["result_description"],
                    amount_cents=parsed["amount_cents"],
                    phone=parsed["phone"],
                    outcome=outcome,
                    suspect_reason=suspect_reason,
                    raw_payload=raw if isinstance(raw, dict) else {},
                    source_ip=source_ip,
                )
        except IntegrityError:
            logger.info(
                "Duplicate M-Pesa callback for %s, already recorded",
                parsed["checkout_request_id"],
            )

        return Response(_ACKNOWLEDGED, status=status.HTTP_200_OK)

    def _record_tenantless(self, parsed, source_ip, raw):
        """A callback whose token matched nothing.

        Written with no tenant, which is why the intent and tenant columns are
        nullable. Under bypass because there is no business to bind.
        """
        try:
            with transaction.atomic(), bypass_rls():
                MpesaCallback.objects.create(
                    tenant=None,
                    intent=None,
                    checkout_request_id=parsed["checkout_request_id"],
                    merchant_request_id=parsed["merchant_request_id"],
                    mpesa_receipt_number=parsed["mpesa_receipt_number"],
                    result_code=parsed["result_code"],
                    result_description=parsed["result_description"],
                    amount_cents=parsed["amount_cents"],
                    phone=parsed["phone"],
                    outcome=CallbackOutcome.SUSPECT,
                    suspect_reason=SuspectReason.UNKNOWN_TOKEN,
                    raw_payload=raw if isinstance(raw, dict) else {},
                    source_ip=source_ip,
                )
        except IntegrityError:
            logger.info("Duplicate unknown-token callback, already recorded")
