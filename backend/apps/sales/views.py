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
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsCashierOrAbove, IsManagerOrAbove
from apps.sales.authorization import (
    DiscountNotAuthorized,
    record_authorization,
    resolve_discount_authorization,
)
from apps.sales.models import Sale
from apps.sales.serializers import (
    CashCheckoutSerializer,
    SaleSerializer,
)
from apps.sales.services import CheckoutError, LineRequest, create_sale, take_cash, void_sale
from apps.sales.states import IllegalTransition
from apps.stores.models import Store


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

                take_cash(
                    sale=sale,
                    tendered_cents=data["tendered_cents"],
                    user=request.user,
                    round_to_shilling=data.get("round_to_shilling", True),
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
                {"detail": exc.detail, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sale.refresh_from_db()
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

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

        Defaults to the business's default branch. With several branches and no
        choice made, refusing beats guessing: a sale filed against the wrong
        branch takes its stock movement with it.
        """
        stores = Store.objects.filter(tenant=request.user.tenant, is_active=True)

        if store_id:
            store = stores.filter(pk=store_id).first()
            if store is None:
                return None, Response(
                    {"detail": "No such branch.", "code": "not_found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return store, None

        if request.user.store_id:
            store = stores.filter(pk=request.user.store_id).first()
            if store is not None:
                return store, None

        default = stores.filter(is_default=True).first()
        if default is not None:
            return default, None

        if stores.count() > 1:
            return None, Response(
                {
                    "detail": (
                        "This business has several branches and none is marked "
                        "default. Say which branch this sale belongs to."
                    ),
                    "code": "store_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        only = stores.first()
        if only is None:
            return None, Response(
                {"detail": "This business has no active branch.", "code": "no_store"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return only, None
