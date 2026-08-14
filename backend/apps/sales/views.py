"""
Checkout endpoints.

These do two jobs and no more: settle who is allowed to do what, and shape the
request. Pricing, state transitions, receipt numbering and stock movement all
belong to ``apps.sales.services``, which stays the single source of truth -
there are three ways into a sale by the end of this milestone (the till, the
sync endpoint, an M-Pesa callback) and each re-implementation would be a place
for the three to disagree.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.compliance.services import ComplianceError, issue_for_settled_sale
from apps.core.permissions import IsCashierOrAbove, IsManagerOrAbove
from apps.payments.services import StkError, initiate_stk
from apps.sales.authorization import (
    DiscountNotAuthorized,
    record_authorization,
    resolve_discount_authorization,
)
from apps.sales.models import Sale
from apps.sales.receipt_render import receipt_filename, render_pdf, render_text
from apps.sales.serializers import (
    CashCheckoutSerializer,
    MpesaCheckoutSerializer,
    PaymentIntentSerializer,
    SaleSerializer,
)
from apps.sales.services import CheckoutError, LineRequest, create_sale, take_cash, void_sale
from apps.sales.states import IllegalTransition
from apps.shifts.services import attribute_payment, resolve_shift
from apps.stores.selection import resolve_store_for


@extend_schema(tags=["sales"])
class SaleViewSet(viewsets.ReadOnlyModelViewSet):
    """Sales, and the ways to make one."""

    serializer_class = SaleSerializer
    permission_classes = [IsCashierOrAbove]
    filterset_fields = ["state", "store"]
    ordering = ["-created_at"]

    def get_queryset(self):
        """Sales of the caller's business, with everything a receipt needs."""
        if getattr(self, "swagger_fake_view", False):
            return Sale.objects.none()
        return (
            Sale.objects.filter(tenant=self.request.user.tenant)
            .select_related("store", "cashier")
            .prefetch_related("lines", "payments", "refunds")
        )

    @extend_schema(
        summary="Ring up and settle a sale in cash",
        description=(
            "Prices the cart, takes the cash and returns a settled sale with its "
            "receipt number - one request, because that is how a counter works. "
            "Splitting it in two would strand an open sale whenever the "
            "connection dropped between them.\n\n"
            "**Discounts need authority.** A manager or owner ringing up "
            "authorises from their own session and sends only a reason. A "
            "cashier must have a manager approve it at the till, by username "
            "and their own PIN or password - an id alone would not be a gate, "
            "since whoever can type the id can type any id.\n\n"
            "Prices come from the catalogue. A supplied price is consulted only "
            "for variable-priced items, and only at or above their marked "
            "price; going below that is a discount, so it leaves a trail."
        ),
        request=CashCheckoutSerializer,
        responses={
            201: SaleSerializer,
            400: OpenApiResponse(description="Refused, with a reason a cashier can act on"),
            403: OpenApiResponse(description="Discount without authority"),
        },
        examples=[
            OpenApiExample(
                "A cashier, with a manager approving the discount",
                value={
                    "lines": [
                        {"item_id": "0f8c...", "quantity": "2", "discount_cents": 2000}
                    ],
                    "discount_authorization": {
                        "username": "mngr",
                        "pin": "4471",
                        "reason": "Damaged packaging",
                    },
                    "tendered_cents": 50000,
                },
                request_only=True,
            ),
            OpenApiExample(
                "A manager ringing up, authorising from their own session",
                value={
                    "lines": [{"item_id": "0f8c...", "quantity": "1"}],
                    "cart_discount_bps": 1000,
                    "discount_authorization": {"reason": "Regular customer"},
                    "tendered_cents": 20000,
                },
                request_only=True,
            ),
        ],
    )
    @action(detail=False, methods=["post"], url_path="checkout/cash")
    def cash_checkout(self, request):
        serializer = CashCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        store, store_error = self._resolve_store(request, data.get("store_id"))
        if store_error is not None:
            return store_error

        # Authority is settled before anything is written, so a refusal leaves
        # no sale, no discount and no partial state behind.
        authorization = None
        if data["_has_discount"]:
            try:
                authorization = resolve_discount_authorization(
                    actor=request.user,
                    payload=data.get("discount_authorization"),
                    request=request,
                )
            except DiscountNotAuthorized as exc:
                return Response(
                    {"detail": exc.detail, "code": exc.code},
                    status=status.HTTP_403_FORBIDDEN,
                )

        lines = [
            LineRequest(
                item_id=str(line["item_id"]),
                quantity=Decimal(str(line["quantity"])),
                unit_price_cents=line.get("unit_price_cents"),
                discount_bps=line.get("discount_bps", 0),
                discount_cents=line.get("discount_cents", 0),
            )
            for line in data["lines"]
        ]

        try:
            with transaction.atomic():
                sale = create_sale(
                    tenant=request.user.tenant,
                    store=store,
                    cashier=request.user,
                    lines=lines,
                    cart_discount_bps=data.get("cart_discount_bps", 0),
                    cart_discount_cents=data.get("cart_discount_cents", 0),
                    client_uuid=data.get("client_uuid"),
                    customer_phone=data.get("customer_phone", ""),
                    note=data.get("note", ""),
                )

                if authorization is not None:
                    for field, value in authorization.as_sale_fields().items():
                        setattr(sale, field, value)
                    sale.save(
                        update_fields=[
                            *authorization.as_sale_fields().keys(),
                            "updated_at",
                        ]
                    )

                payment = take_cash(
                    sale=sale,
                    tendered_cents=data["tendered_cents"],
                    user=request.user,
                    round_to_shilling=data.get("round_to_shilling", True),
                )

                # Which drawer this cash went into. Done here rather than
                # inside take_cash so that apps.sales stays unaware of shifts -
                # the drawer is a thing that watches sales, not the other way
                # round. A business that runs no shifts gets None and nothing
                # changes.
                attribute_payment(
                    payment=payment,
                    shift=resolve_shift(tenant=request.user.tenant, user=request.user),
                )

                # The tax document, raised inside this same transaction so its
                # invoice number rolls back with the sale. There is no deferred
                # path for a live sale: the number is taken now, in the sale's
                # own transaction, exactly as the receipt number is.
                sale.refresh_from_db()
                issue_for_settled_sale(
                    sale=sale,
                    buyer_pin=data.get("buyer_pin", ""),
                    user=request.user,
                    request=request,
                )

                if authorization is not None:
                    record_authorization(
                        sale=sale,
                        authorization=authorization,
                        actor=request.user,
                        request=request,
                    )
        except (CheckoutError, ComplianceError) as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sale.refresh_from_db()
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Ring up a sale and prompt the customer to pay by M-Pesa",
        description=(
            "Prices the cart the same way the cash route does, then sends an "
            "STK push. The sale comes back **awaiting payment**, not paid - the "
            "customer still has to enter their PIN, and the receipt number is "
            "allocated when the money actually lands.\n\n"
            "Discounts need exactly the same authority as on the cash route.\n\n"
            "Refuses with 409 while a prompt is already waiting on this sale. "
            "Two prompts answered by an obliging customer are two real "
            "payments, and nothing downstream can un-take money that was sent."
        ),
        request=MpesaCheckoutSerializer,
        responses={
            202: SaleSerializer,
            400: OpenApiResponse(description="Refused"),
            403: OpenApiResponse(description="Discount without authority"),
            409: OpenApiResponse(description="A prompt is already waiting on this sale"),
        },
    )
    @action(detail=False, methods=["post"], url_path="checkout/mpesa")
    def mpesa_checkout(self, request):
        serializer = MpesaCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        store, store_error = self._resolve_store(request, data.get("store_id"))
        if store_error is not None:
            return store_error

        # The same gate as cash, called the same way. A discount is a discount
        # however the customer pays.
        authorization = None
        if data["_has_discount"]:
            try:
                authorization = resolve_discount_authorization(
                    actor=request.user,
                    payload=data.get("discount_authorization"),
                    request=request,
                )
            except DiscountNotAuthorized as exc:
                return Response(
                    {"detail": exc.detail, "code": exc.code},
                    status=status.HTTP_403_FORBIDDEN,
                )

        lines = [
            LineRequest(
                item_id=str(line["item_id"]),
                quantity=Decimal(str(line["quantity"])),
                unit_price_cents=line.get("unit_price_cents"),
                discount_bps=line.get("discount_bps", 0),
                discount_cents=line.get("discount_cents", 0),
            )
            for line in data["lines"]
        ]

        try:
            with transaction.atomic():
                sale = create_sale(
                    tenant=request.user.tenant,
                    store=store,
                    cashier=request.user,
                    lines=lines,
                    cart_discount_bps=data.get("cart_discount_bps", 0),
                    cart_discount_cents=data.get("cart_discount_cents", 0),
                    client_uuid=data.get("client_uuid"),
                    customer_phone=data["phone"],
                    note=data.get("note", ""),
                )

                if authorization is not None:
                    for field, value in authorization.as_sale_fields().items():
                        setattr(sale, field, value)
                    sale.save(
                        update_fields=[*authorization.as_sale_fields().keys(), "updated_at"]
                    )

                intent = initiate_stk(
                    sale=sale,
                    phone=data["phone"],
                    user=request.user,
                    client_uuid=data.get("payment_client_uuid"),
                )

                if authorization is not None:
                    record_authorization(
                        sale=sale,
                        authorization=authorization,
                        actor=request.user,
                        request=request,
                    )
        except CheckoutError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
            )
        except StkError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code}, status=exc.status
            )

        sale.refresh_from_db()
        body = SaleSerializer(sale, context=self.get_serializer_context()).data
        body["payment_intent"] = PaymentIntentSerializer(intent).data
        return Response(body, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="The receipt for a sale",
        description=(
            "Two renderings from the same source, so they cannot disagree about "
            "what was sold. `?format=pdf` returns a PDF sized like a till roll; "
            "the default returns plain text laid out for a 58mm thermal "
            "printer, which the till sends over Bluetooth as ESC/POS.\n\n"
            "Reads the sale's own snapshotted lines, so a receipt reprinted next "
            "year shows the price that was charged rather than today's."
        ),
        responses={200: OpenApiResponse(description="text/plain")},
    )
    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        return HttpResponse(
            render_text(self.get_object()), content_type="text/plain; charset=utf-8"
        )

    @extend_schema(
        summary="The receipt for a sale, as a PDF",
        description=(
            "The same receipt as the text route, rendered on a page sized like a "
            "till roll rather than A4 - so a shop can print it on either without "
            "the text stranded in the corner of a mostly empty sheet.\n\n"
            "A separate route rather than a `?format=` parameter: DRF reserves "
            "that name for its own content negotiation, and overloading it makes "
            "the endpoint 404 for a reason nobody would guess."
        ),
        responses={200: OpenApiResponse(description="application/pdf")},
    )
    @action(detail=True, methods=["get"], url_path="receipt/pdf")
    def receipt_pdf(self, request, pk=None):
        sale = self.get_object()
        response = HttpResponse(render_pdf(sale), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{receipt_filename(sale)}"'
        return response

    @extend_schema(
        summary="Void an unpaid sale",
        description=(
            "Abandons a sale on which nothing has settled. A sale that has taken "
            "money cannot be voided - the correction is a refund, which records "
            "an amount, an actor and a reason. The ledger is checked as well as "
            "the state, so this holds even if the cached state were wrong."
        ),
        request={
            "application/json": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            }
        },
        responses={200: SaleSerializer, 400: OpenApiResponse(description="Refused")},
    )
    @action(detail=True, methods=["post"], permission_classes=[IsManagerOrAbove])
    def void(self, request, pk=None):
        sale = self.get_object()
        try:
            void_sale(sale=sale, user=request.user, reason=request.data.get("reason", ""))
        except CheckoutError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
            )
        except IllegalTransition as exc:
            # Named rather than caught broadly on purpose. A bare `except
            # Exception` here would turn a genuine bug into a polite 400,
            # reporting our own fault as the caller's bad input - and on an
            # endpoint that moves money, a bug that looks like a validation
            # error is a bug nobody investigates.
            return Response(
                {"detail": exc.detail, "code": "illegal_transition"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sale.refresh_from_db()
        return Response(SaleSerializer(sale, context=self.get_serializer_context()).data)

    def _resolve_store(self, request, store_id):
        """Which branch this sale belongs to.

        Delegates to apps.stores.selection, which the sync endpoint uses too -
        a branch resolved one way at the till and another way at sync would
        file the same sale's stock movement against different shops depending
        on whether the network was up.
        """
        return resolve_store_for(request, store_id)
