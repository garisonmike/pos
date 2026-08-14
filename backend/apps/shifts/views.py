"""
Opening and closing a drawer.

The blind count is enforced here, not merely presented. ``current`` returns the
open shift with its closing figures null, and there is no endpoint that returns
an expected total for a drawer still open. A cashier who wanted to see the
figure before declaring theirs would have to guess it.
"""

from __future__ import annotations

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.models import Device
from apps.core.permissions import IsCashierOrAbove
from apps.shifts.models import Shift
from apps.shifts.serializers import (
    CashMovementReadSerializer,
    CashMovementSerializer,
    CloseShiftSerializer,
    OpenShiftSerializer,
    ShiftSerializer,
)
from apps.shifts.services import (
    ShiftError,
    close_shift,
    may_close,
    open_shift,
    open_shift_for,
    record_cash_movement,
)
from apps.stores.selection import resolve_store_for


@extend_schema(tags=["shifts"])
class ShiftViewSet(viewsets.ReadOnlyModelViewSet):
    """Drawers, and what happened to them."""

    serializer_class = ShiftSerializer
    permission_classes = [IsCashierOrAbove]
    filterset_fields = ["state", "store", "cashier"]
    ordering = ["-opened_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Shift.objects.none()
        return (
            Shift.objects.filter(tenant=self.request.user.tenant)
            .select_related("store", "cashier", "closed_by")
            .prefetch_related("movements", "discrepancies")
        )

    @extend_schema(
        summary="The drawer this person currently has open",
        responses={200: ShiftSerializer},
    )
    @action(detail=False, methods=["get"], url_path="current")
    def current(self, request):
        """What the till shows on the sell screen.

        Returns the shift with its closing figures null, because it is open.
        There is deliberately no endpoint that reports what an open drawer is
        expected to hold - that figure exists only after somebody has counted.
        """
        shift = open_shift_for(request.user)
        if shift is None:
            return Response({"shift": None})
        return Response(
            {"shift": self.get_serializer(shift).data}
        )

    @extend_schema(
        summary="Open a drawer",
        request=OpenShiftSerializer,
        responses={201: ShiftSerializer},
    )
    @action(detail=False, methods=["post"], url_path="open")
    def open(self, request):
        serializer = OpenShiftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        store, store_error = resolve_store_for(request, data.get("store_id"))
        if store_error is not None:
            return store_error

        device = None
        if data.get("device_id"):
            # Tenant-scoped, so another business's till is simply not found.
            device = Device.objects.filter(pk=data["device_id"], is_active=True).first()
            if device is None:
                return Response(
                    {
                        "detail": "That till is not registered to this business.",
                        "code": "unknown_device",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            with transaction.atomic():
                shift = open_shift(
                    tenant=request.user.tenant,
                    store=store,
                    cashier=request.user,
                    device=device,
                    opening_float_cents=data["opening_float_cents"],
                    client_uuid=data.get("client_uuid"),
                    note=data.get("note", ""),
                    request=request,
                )
        except ShiftError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            self.get_serializer(shift).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Count the drawer and close it",
        description=(
            "The request carries the counted figure and nothing else. The "
            "expected total is computed only once the count is in hand and is "
            "returned on this response - a cashier cannot see it beforehand, "
            "which is what stops the count being the expectation typed back."
        ),
        request=CloseShiftSerializer,
        responses={200: ShiftSerializer},
    )
    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None):
        shift = self.get_object()

        if not may_close(shift=shift, user=request.user):
            # A cashier closing a colleague's drawer would be putting a figure
            # against another person's name.
            return Response(
                {
                    "detail": "Only a manager can close somebody else's drawer.",
                    "code": "not_your_shift",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CloseShiftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            with transaction.atomic():
                shift = close_shift(
                    shift=shift,
                    declared_closing_cents=data["declared_closing_cents"],
                    closed_by=request.user,
                    note=data.get("note", ""),
                    denominations=data.get("denominations"),
                    request=request,
                )
        except ShiftError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        shift.refresh_from_db()
        return Response(self.get_serializer(shift).data)

    @extend_schema(
        summary="Record cash in or out of the drawer",
        request=CashMovementSerializer,
        responses={201: CashMovementReadSerializer},
    )
    @action(detail=True, methods=["post"], url_path="cash")
    def cash(self, request, pk=None):
        shift = self.get_object()

        if shift.cashier_id != request.user.id and not may_close(
            shift=shift, user=request.user
        ):
            return Response(
                {
                    "detail": "That is not your drawer.",
                    "code": "not_your_shift",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CashMovementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            with transaction.atomic():
                movement = record_cash_movement(
                    shift=shift,
                    kind=data["kind"],
                    amount_cents=data["amount_cents"],
                    reason=data["reason"],
                    user=request.user,
                    request=request,
                )
        except ShiftError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            CashMovementReadSerializer(movement).data,
            status=status.HTTP_201_CREATED,
        )
