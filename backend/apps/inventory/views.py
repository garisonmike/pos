"""
Stock endpoints.

Reading stock is open to everyone in the shop, because a cashier needs to know
whether there is any left. Changing it is manager and above, and every change
carries a reason that goes into the audit trail alongside the ledger entry.

That pairing is deliberate and worth keeping: adjusting stock is how a
dishonest employee would cover a theft, so the role boundary and the record of
crossing it belong together.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.permissions import IsManagerOrAbove, ReadOnlyOrManager
from apps.inventory.models import MovementReason, StockItem, StockMovement, apply_movement
from apps.inventory.serializers import (
    StockAdjustSerializer,
    StockItemCreateSerializer,
    StockItemSerializer,
    StockMovementSerializer,
)


@extend_schema(tags=["inventory"])
class StockItemViewSet(viewsets.ModelViewSet):
    """Stock levels, and the ledger behind each one."""

    permission_classes = [ReadOnlyOrManager]
    http_method_names = ["get", "post", "patch", "head", "options"]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["item__name", "item__sku"]
    ordering_fields = ["quantity", "item__name", "updated_at"]
    ordering = ["item__name"]
    filterset_fields = ["store", "item"]

    def get_queryset(self):
        """Stock of the caller's business, with the item and branch joined in."""
        if getattr(self, "swagger_fake_view", False):
            return StockItem.objects.none()
        return StockItem.objects.filter(
            tenant=self.request.user.tenant
        ).select_related("item", "store")

    def get_serializer_class(self):
        if self.action == "create":
            return StockItemCreateSerializer
        if self.action == "adjust":
            return StockAdjustSerializer
        return StockItemSerializer

    @extend_schema(
        summary="Start tracking an item at a branch",
        description=(
            "Creates a stock record. Any opening quantity is written as a "
            "movement rather than straight into the total, so even the first "
            "figure has an explanation behind it."
        ),
        request=StockItemCreateSerializer,
        responses={201: StockItemSerializer},
    )
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        opening = serializer.validated_data.pop("opening_quantity", Decimal("0"))
        stock_item = StockItem.objects.create(
            tenant=request.user.tenant, **serializer.validated_data
        )

        if opening:
            apply_movement(
                stock_item=stock_item,
                delta=opening,
                reason=MovementReason.COUNT,
                user=request.user,
                note="Opening quantity when tracking started.",
            )
            stock_item.refresh_from_db()

        record_audit(
            action=AuditAction.CREATE,
            entity=stock_item,
            actor=request.user,
            request=request,
            after={
                "item": str(stock_item.item_id),
                "store": stock_item.store.code,
                "opening_quantity": str(opening),
            },
        )
        return Response(
            StockItemSerializer(stock_item).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Adjust a stock level",
        description=(
            "Moves stock and records why. Give either a `delta` or a "
            "`new_quantity` - counting a shelf naturally produces the second, "
            "receiving a delivery the first.\n\n"
            "A note is required for adjustments, wastage and count "
            "corrections. Stock that moves without an explanation cannot be "
            "reconciled afterwards, which is the whole reason for the ledger.\n\n"
            "The result may be negative. Refusing that would mean refusing to "
            "record something that has already happened; it is surfaced as "
            "`is_negative` for a manager to reconcile instead."
        ),
        request=StockAdjustSerializer,
        responses={
            200: StockItemSerializer,
            400: OpenApiResponse(description="Missing reason, or nothing to change"),
            403: OpenApiResponse(description="Requires a manager or the owner"),
        },
    )
    @action(detail=True, methods=["post"], permission_classes=[IsManagerOrAbove])
    @transaction.atomic
    def adjust(self, request, pk=None):
        stock_item = self.get_object()
        serializer = StockAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        before_quantity = stock_item.quantity
        delta = data.get("delta")
        if delta is None:
            delta = data["new_quantity"] - before_quantity
            if delta == 0:
                return Response(
                    {
                        "detail": "The count already matches. Nothing to change.",
                        "code": "no_change",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        movement = apply_movement(
            stock_item=stock_item,
            delta=delta,
            reason=data["reason"],
            user=request.user,
            note=data.get("note", ""),
        )

        if data["reason"] == MovementReason.COUNT:
            StockItem.objects.filter(pk=stock_item.pk).update(
                last_counted_at=timezone.now()
            )

        stock_item.refresh_from_db()

        record_audit(
            action=AuditAction.STOCK_ADJUST,
            entity=stock_item,
            actor=request.user,
            request=request,
            reason=data.get("note", ""),
            before={"quantity": str(before_quantity)},
            after={
                "quantity": str(stock_item.quantity),
                "delta": str(delta),
                "movement_reason": data["reason"],
                "movement": str(movement.pk),
            },
        )

        body = StockItemSerializer(stock_item).data
        if stock_item.is_negative:
            body["warning"] = (
                "This branch now shows negative stock, which means the records "
                "disagree with the shelf somewhere. Count it and correct."
            )
        return Response(body)

    @extend_schema(
        summary="The ledger for one stock level",
        description=(
            "Every movement, newest first, with the balance after each. This is "
            "the record that answers why a count is what it is."
        ),
        responses={200: StockMovementSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def movements(self, request, pk=None):
        stock_item = self.get_object()
        queryset = (
            StockMovement.objects.filter(stock_item=stock_item)
            .select_related("user", "stock_item__item", "stock_item__store")
            .order_by("-created_at")
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(StockMovementSerializer(page, many=True).data)
        return Response(StockMovementSerializer(queryset, many=True).data)

    @extend_schema(
        summary="Stock at or below its reorder level",
        description=(
            "What needs reordering. A reorder level of zero means 'do not warn "
            "me' rather than 'always warn me' - otherwise every unconfigured "
            "item would sit here permanently and the list would be ignored."
        ),
        parameters=[
            OpenApiParameter("store", str, description="Limit to one branch."),
        ],
        responses={200: StockItemSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def low(self, request):
        from django.db.models import F

        queryset = (
            self.get_queryset()
            .filter(reorder_level__gt=0, quantity__lte=F("reorder_level"))
            .order_by("quantity")
        )
        store = request.query_params.get("store")
        if store:
            queryset = queryset.filter(store_id=store)

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(StockItemSerializer(page, many=True).data)
        return Response(StockItemSerializer(queryset, many=True).data)
