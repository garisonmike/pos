"""
The wire shapes for tables, orders and tickets.

Read shapes carry what a waiter's screen needs in one request - a table list
with what each table owes, an order with its lines and their modifiers - because
a person standing at a table should not wait on three round trips.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.restaurant.models import (
    KitchenTicket,
    Modifier,
    ModifierGroup,
    Order,
    OrderLine,
    OrderLineModifier,
    Table,
)


class ModifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Modifier
        fields = ("id", "name", "price_cents", "position")
        read_only_fields = fields


class ModifierGroupSerializer(serializers.ModelSerializer):
    modifiers = ModifierSerializer(many=True, read_only=True)
    is_required = serializers.BooleanField(read_only=True)

    class Meta:
        model = ModifierGroup
        fields = (
            "id",
            "name",
            "min_choices",
            "max_choices",
            "is_required",
            "position",
            "modifiers",
        )
        read_only_fields = fields


class OrderLineModifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderLineModifier
        fields = ("id", "name", "price_cents")
        read_only_fields = fields


class OrderLineSerializer(serializers.ModelSerializer):
    modifiers = OrderLineModifierSerializer(many=True, read_only=True)
    unit_price_cents = serializers.IntegerField(read_only=True)
    has_been_sent = serializers.SerializerMethodField()

    class Meta:
        model = OrderLine
        fields = (
            "id",
            "item",
            "name",
            "unit",
            "quantity",
            "base_price_cents",
            "unit_price_cents",
            "note",
            "modifiers",
            "is_voided",
            "void_reason",
            "has_been_sent",
            "created_at",
        )
        read_only_fields = fields

    def get_has_been_sent(self, obj: OrderLine) -> bool:
        """Whether the kitchen already has this.

        The fact a waiter needs before striking a line off: cancelling
        something nobody has started is free, and cancelling something already
        on the pass is a conversation.
        """
        return obj.first_ticket_id is not None


class OrderSerializer(serializers.ModelSerializer):
    lines = OrderLineSerializer(many=True, read_only=True)
    table_name = serializers.CharField(source="table.name", read_only=True, default=None)
    opened_by_username = serializers.CharField(
        source="opened_by.username", read_only=True
    )
    subtotal_cents = serializers.SerializerMethodField()
    ticket_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "id",
            "state",
            "store",
            "table",
            "table_name",
            "opened_by",
            "opened_by_username",
            "opened_at",
            "closed_at",
            "covers",
            "note",
            "void_reason",
            "sale",
            "merged_into",
            "subtotal_cents",
            "ticket_count",
            "lines",
        )
        read_only_fields = fields

    def get_subtotal_cents(self, obj: Order) -> int:
        """A running total for the waiter's screen.

        **Indicative, not the bill.** The bill is priced by the server at
        payment, through the same code a duka's checkout uses. This figure has
        no tax treatment and no rounding applied, and a screen showing it should
        say so rather than presenting it as an amount due.
        """
        return sum(
            int(line.unit_price_cents * line.quantity)
            for line in obj.lines.all()
            if not line.is_voided
        )

    def get_ticket_count(self, obj: Order) -> int:
        return obj.tickets.count()


class TableSerializer(serializers.ModelSerializer):
    """A table, with whether anybody is sitting at it."""

    open_order_id = serializers.SerializerMethodField()
    is_occupied = serializers.SerializerMethodField()

    class Meta:
        model = Table
        fields = (
            "id",
            "store",
            "name",
            "seats",
            "is_active",
            "is_occupied",
            "open_order_id",
        )
        read_only_fields = ("id", "is_occupied", "open_order_id")

    def _live(self, obj: Table):
        # Prefetched by the viewset, so a floor of thirty tables is one query
        # rather than thirty.
        live = [order for order in obj.orders.all() if order.is_live]
        return live[0] if live else None

    def get_open_order_id(self, obj: Table):
        order = self._live(obj)
        return str(order.id) if order else None

    def get_is_occupied(self, obj: Table) -> bool:
        return self._live(obj) is not None


class KitchenTicketSerializer(serializers.ModelSerializer):
    lines = OrderLineSerializer(many=True, read_only=True)
    table_name = serializers.CharField(
        source="order.table.name", read_only=True, default=None
    )

    class Meta:
        model = KitchenTicket
        fields = (
            "id",
            "order",
            "table_name",
            "sequence",
            "printed_at",
            "reprint_count",
            "lines",
        )
        read_only_fields = fields


# ---- Write shapes -------------------------------------------------------


class OpenOrderSerializer(serializers.Serializer):
    table_id = serializers.UUIDField(required=False, allow_null=True)
    covers = serializers.IntegerField(required=False, default=0, min_value=0)
    note = serializers.CharField(required=False, allow_blank=True)


class AddLineSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    modifier_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list
    )
    note = serializers.CharField(required=False, allow_blank=True)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("A quantity must be more than zero.")
        return value


class VoidSerializer(serializers.Serializer):
    """Cancelling something, with the authority it may need.

    The credential block is the *same shape* the discount gate takes, because
    it is the same mechanism. A manager or owner cancelling sends only a
    reason; anybody else needs a manager's username and credential, verified
    right now.
    """

    reason = serializers.CharField(max_length=500)
    username = serializers.CharField(max_length=64, required=False, allow_blank=True)
    pin = serializers.CharField(write_only=True, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)


class MoveOrderSerializer(serializers.Serializer):
    table_id = serializers.UUIDField()


class MergeOrderSerializer(serializers.Serializer):
    into_order_id = serializers.UUIDField()


class BillOrderSerializer(serializers.Serializer):
    """Settling a table in cash.

    Deliberately the same shape the retail checkout takes for the money part,
    because it *is* the retail checkout - the order is converted and everything
    downstream is unchanged.
    """

    tendered_cents = serializers.IntegerField(min_value=0)
    round_to_shilling = serializers.BooleanField(default=True)
    buyer_pin = serializers.CharField(max_length=20, required=False, allow_blank=True)
