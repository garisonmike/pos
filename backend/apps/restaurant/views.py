"""
The endpoints a waiter's tablet talks to.

**Server-side only for this milestone.** There is no offline order queue: a
restaurant with one till and a wired router is the realistic first customer, and
order state shared across several tablets with no connection is a
distributed-systems problem this module is not taking on alongside everything
else. The till says so explicitly when it cannot reach the server rather than
spinning - a waiter losing a half-typed order to a silent failure is the thing
that guard exists to prevent.

Every route requires the restaurant module. The check is in the service layer
too, because the module boundary is a data question rather than a routing one.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.compliance.services import ComplianceError, issue_for_settled_sale
from apps.core.permissions import IsCashierOrAbove, IsManagerOrAbove
from apps.restaurant.models import (
    KitchenTicket,
    ModifierGroup,
    Order,
    OrderLine,
    OrderState,
    Table,
)
from apps.restaurant.serializers import (
    AddLineSerializer,
    BillOrderSerializer,
    KitchenTicketSerializer,
    MergeOrderSerializer,
    ModifierGroupSerializer,
    MoveOrderSerializer,
    OpenOrderSerializer,
    OrderLineSerializer,
    OrderSerializer,
    TableSerializer,
    VoidSerializer,
)
from apps.restaurant.services import (
    OrderError,
    add_line,
    bill_order,
    close_order,
    merge_orders,
    move_order,
    open_order,
    reprint_ticket,
    require_module,
    send_to_kitchen,
    void_line,
    void_order,
)
from apps.sales.serializers import SaleSerializer
from apps.sales.services import CheckoutError, take_cash
from apps.shifts.services import attribute_payment, resolve_shift
from apps.stores.selection import resolve_store_for


def _refuse(exc) -> Response:
    return Response(
        {"detail": exc.detail, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST
    )


class _RestaurantView(viewsets.ModelViewSet):
    permission_classes = [IsCashierOrAbove]

    def initial(self, request, *args, **kwargs):
        """Refuse a business that has not switched the module on.

        Before anything else runs, so a duka that found the URL gets a clear
        refusal rather than an empty list that looks like a working feature
        with no data in it.
        """
        super().initial(request, *args, **kwargs)
        if not getattr(self, "swagger_fake_view", False):
            require_module(request.user.tenant)


@extend_schema(tags=["restaurant"])
class TableViewSet(_RestaurantView):
    """The floor."""

    serializer_class = TableSerializer
    filterset_fields = ["store", "is_active"]
    ordering = ["name"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Table.objects.none()
        return (
            Table.objects.filter(tenant=self.request.user.tenant)
            .prefetch_related("orders")
            .order_by("name")
        )

    def get_permissions(self):
        # Adding or retiring a table changes the shape of the floor, which is
        # not a waiter's decision.
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsManagerOrAbove()]
        return [IsCashierOrAbove()]

    def perform_create(self, serializer):
        store, error = resolve_store_for(self.request, self.request.data.get("store"))
        if error is not None:
            raise OrderError("Say which branch this table is at.", "store_required")
        serializer.save(tenant=self.request.user.tenant, store=store)


@extend_schema(tags=["restaurant"])
class ModifierGroupViewSet(_RestaurantView):
    """The questions the kitchen needs answered."""

    serializer_class = ModifierGroupSerializer
    ordering = ["position", "name"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ModifierGroup.objects.none()
        queryset = ModifierGroup.objects.filter(
            tenant=self.request.user.tenant
        ).prefetch_related("modifiers")

        item = self.request.query_params.get("item")
        if item:
            queryset = queryset.filter(items=item)
        return queryset.order_by("position", "name")

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsManagerOrAbove()]
        return [IsCashierOrAbove()]


@extend_schema(tags=["restaurant"])
class OrderViewSet(_RestaurantView):
    """What tables have asked for."""

    serializer_class = OrderSerializer
    filterset_fields = ["state", "table", "store"]
    ordering = ["-opened_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        return (
            Order.objects.filter(tenant=self.request.user.tenant)
            .select_related("table", "opened_by")
            .prefetch_related("lines__modifiers", "tickets")
            .order_by("-opened_at")
        )

    def create(self, request, *args, **kwargs):
        serializer = OpenOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        store, store_error = resolve_store_for(request)
        if store_error is not None:
            return store_error

        table = None
        if data.get("table_id"):
            table = Table.objects.filter(pk=data["table_id"]).first()
            if table is None:
                return Response(
                    {"detail": "No such table.", "code": "table_not_found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            order = open_order(
                tenant=request.user.tenant,
                store=store,
                table=table,
                user=request.user,
                covers=data.get("covers", 0),
                note=data.get("note", ""),
                request=request,
            )
        except OrderError as exc:
            return _refuse(exc)

        return Response(
            self.get_serializer(order).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(summary="Add something to an order", request=AddLineSerializer)
    @action(detail=True, methods=["post"], url_path="lines")
    def add_line(self, request, pk=None):
        order = self.get_object()
        serializer = AddLineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            line = add_line(
                order=order,
                item_id=str(data["item_id"]),
                quantity=Decimal(str(data["quantity"])),
                modifier_ids=[str(value) for value in data.get("modifier_ids", [])],
                note=data.get("note", ""),
                user=request.user,
                request=request,
            )
        except OrderError as exc:
            return _refuse(exc)

        return Response(
            OrderLineSerializer(line).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(summary="Strike a line off", request=VoidSerializer)
    @action(detail=True, methods=["post"], url_path=r"lines/(?P<line_id>[^/.]+)/void")
    def void_line(self, request, pk=None, line_id=None):
        order = self.get_object()
        line = OrderLine.objects.filter(pk=line_id, order=order).first()
        if line is None:
            return Response(
                {"detail": "No such line on that order.", "code": "line_not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            void_line(
                line=line,
                user=request.user,
                reason=serializer.validated_data["reason"],
                request=request,
            )
        except OrderError as exc:
            return _refuse(exc)

        return Response(self.get_serializer(order).data)

    @extend_schema(summary="Send what is new to the kitchen")
    @action(detail=True, methods=["post"], url_path="send")
    def send(self, request, pk=None):
        order = self.get_object()
        try:
            ticket = send_to_kitchen(order=order, user=request.user, request=request)
        except OrderError as exc:
            return _refuse(exc)

        return Response(
            KitchenTicketSerializer(ticket).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Cancel a whole order",
        description=(
            "Once the kitchen has been told, this needs a manager's authority - "
            "the ingredients are already spent, and the same mechanism the "
            "discount gate uses decides who may do it. Before any ticket has "
            "printed, nothing has been cooked and no authority is required."
        ),
        request=VoidSerializer,
    )
    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request, pk=None):
        order = self.get_object()
        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            void_order(
                order=order,
                user=request.user,
                reason=data["reason"],
                payload={
                    "username": data.get("username", ""),
                    "pin": data.get("pin", ""),
                    "password": data.get("password", ""),
                },
                request=request,
            )
        except OrderError as exc:
            # A refused authorisation is a 403, not a 400: the request was
            # understood and the answer is "not you".
            if exc.code in (
                "discount_not_authorized",
                "discount_authorization_required",
                "discount_reason_required",
            ):
                return Response(
                    {"detail": exc.detail, "code": exc.code},
                    status=status.HTTP_403_FORBIDDEN,
                )
            return _refuse(exc)

        return Response(self.get_serializer(order).data)

    @extend_schema(summary="Move an order to another table", request=MoveOrderSerializer)
    @action(detail=True, methods=["post"], url_path="move")
    def move(self, request, pk=None):
        order = self.get_object()
        serializer = MoveOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        table = Table.objects.filter(pk=serializer.validated_data["table_id"]).first()
        if table is None:
            return Response(
                {"detail": "No such table.", "code": "table_not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            move_order(order=order, table=table, user=request.user, request=request)
        except OrderError as exc:
            return _refuse(exc)

        return Response(self.get_serializer(order).data)

    @extend_schema(summary="Merge this order into another", request=MergeOrderSerializer)
    @action(detail=True, methods=["post"], url_path="merge")
    def merge(self, request, pk=None):
        order = self.get_object()
        serializer = MergeOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target = self.get_queryset().filter(
            pk=serializer.validated_data["into_order_id"]
        ).first()
        if target is None:
            return Response(
                {"detail": "No such order.", "code": "order_not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            merged = merge_orders(
                source=order, target=target, user=request.user, request=request
            )
        except OrderError as exc:
            return _refuse(exc)

        # Re-read rather than serialize the instance we were handed: its lines
        # were prefetched before the merge moved rows into it, so the cached
        # relation is a snapshot of the order as it was a moment ago.
        merged = self.get_queryset().get(pk=merged.pk)
        return Response(self.get_serializer(merged).data)

    @extend_schema(
        summary="Bill a table in cash",
        description=(
            "Converts the order to an ordinary sale and settles it. Everything "
            "downstream - receipt, tax document, shift attribution, reporting - "
            "happens through exactly the code a retail checkout uses."
        ),
        request=BillOrderSerializer,
        responses={201: SaleSerializer},
    )
    @action(detail=True, methods=["post"], url_path="bill")
    def bill(self, request, pk=None):
        order = self.get_object()
        serializer = BillOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            with transaction.atomic():
                sale = bill_order(order=order, user=request.user, request=request)

                payment = take_cash(
                    sale=sale,
                    tendered_cents=data["tendered_cents"],
                    user=request.user,
                    round_to_shilling=data.get("round_to_shilling", True),
                )
                attribute_payment(
                    payment=payment,
                    shift=resolve_shift(tenant=request.user.tenant, user=request.user),
                )
                sale.refresh_from_db()
                issue_for_settled_sale(
                    sale=sale,
                    buyer_pin=data.get("buyer_pin", ""),
                    user=request.user,
                    request=request,
                )
                close_order(order=order, user=request.user, request=request)
        except (OrderError, CheckoutError, ComplianceError) as exc:
            return _refuse(exc)

        sale.refresh_from_db()
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(summary="Tables currently owing money")
    @action(detail=False, methods=["get"], url_path="open")
    def open_orders(self, request):
        orders = self.get_queryset().filter(
            state__in=(OrderState.OPEN, OrderState.SENT)
        )
        return Response(self.get_serializer(orders, many=True).data)


@extend_schema(tags=["restaurant"])
class KitchenTicketViewSet(_RestaurantView):
    """What the kitchen has been told.

    Read-only apart from a reprint. A ticket is a record of an instruction that
    was given; editing one would be rewriting what the kitchen was asked for.
    """

    serializer_class = KitchenTicketSerializer
    http_method_names = ["get", "post", "head", "options"]
    filterset_fields = ["order"]
    ordering = ["-printed_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return KitchenTicket.objects.none()
        return (
            KitchenTicket.objects.filter(tenant=self.request.user.tenant)
            .select_related("order", "order__table")
            .prefetch_related("lines__modifiers")
            .order_by("-printed_at")
        )

    @extend_schema(
        summary="Print a ticket again",
        description=(
            "Reprints **that** ticket, exactly as it was - not everything new "
            "since. A reprint that behaved like a fresh send would have the "
            "kitchen cook a different set of food from the one the waiter "
            "asked for."
        ),
    )
    @action(detail=True, methods=["post"], url_path="reprint")
    def reprint(self, request, pk=None):
        ticket = reprint_ticket(ticket=self.get_object())
        return Response(self.get_serializer(ticket).data)
