"""Serializers for stock levels and the movements behind them."""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import Item
from apps.inventory.models import (
    REASONS_REQUIRING_NOTE,
    MovementReason,
    StockItem,
    StockMovement,
)
from apps.stores.models import Store


class StockItemSerializer(serializers.ModelSerializer):
    """How much of one item is held at one branch."""

    item_name = serializers.CharField(source="item.name", read_only=True)
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_unit = serializers.CharField(source="item.unit", read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    is_low = serializers.BooleanField(read_only=True)
    is_negative = serializers.BooleanField(read_only=True)

    class Meta:
        model = StockItem
        fields = (
            "id",
            "item",
            "item_name",
            "item_sku",
            "item_unit",
            "store",
            "store_code",
            "quantity",
            "reorder_level",
            "is_low",
            "is_negative",
            "last_counted_at",
            "updated_at",
        )
        # Quantity is deliberately read-only. It only ever changes through a
        # movement, so that every change carries a reason and an author; a
        # writable field here would be a way to alter stock leaving no trace.
        read_only_fields = ("id", "quantity", "last_counted_at", "updated_at")


class StockItemCreateSerializer(serializers.ModelSerializer):
    """Start tracking an item at a branch."""

    opening_quantity = serializers.DecimalField(
        max_digits=12,
        decimal_places=3,
        required=False,
        default=Decimal("0"),
        help_text=(
            "Stock on hand at the moment tracking starts. Recorded as a "
            "movement rather than written straight into the quantity, so even "
            "the opening figure has a trail."
        ),
    )

    class Meta:
        model = StockItem
        fields = ("id", "item", "store", "reorder_level", "opening_quantity")
        read_only_fields = ("id",)

    def validate_item(self, value: Item) -> Item:
        tenant = self.context["request"].user.tenant
        if value.tenant_id != tenant.id:
            raise serializers.ValidationError("No such item.")
        if not value.track_stock:
            raise serializers.ValidationError(
                f"{value.name} is not stock-tracked. Services and made-to-order "
                "items have no shelf to count; turn stock tracking on first."
            )
        return value

    def validate_store(self, value: Store) -> Store:
        if value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("No such branch.")
        return value

    def validate(self, attrs: dict) -> dict:
        exists = StockItem.objects.filter(
            tenant=self.context["request"].user.tenant,
            item=attrs["item"],
            store=attrs["store"],
        ).exists()
        if exists:
            raise serializers.ValidationError(
                {"detail": "That item is already tracked at that branch. Adjust it instead."}
            )
        return attrs


class StockAdjustSerializer(serializers.Serializer):
    """A manual change to a stock level.

    Either say how much to move by (``delta``) or what the count should now be
    (``new_quantity``). Counting a shelf gives you the second; receiving a
    delivery gives you the first, and making the caller convert between them is
    how off-by-one errors get into stock records.
    """

    delta = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False,
        help_text="Signed change. Negative removes stock.",
    )
    new_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False,
        help_text="What the count should be after this. Use when counting a shelf.",
    )
    reason = serializers.ChoiceField(choices=MovementReason.choices)
    note = serializers.CharField(
        allow_blank=True,
        required=False,
        default="",
        help_text="Why. Required for adjustments, wastage and count corrections.",
    )

    def validate_reason(self, value: str) -> str:
        """Sales and refunds are not adjustments.

        Those move stock as a side effect of a sale and carry its reference.
        Allowing them here would let someone fabricate a sale's stock effect
        without a sale, which is exactly the hole an audit trail exists to close.
        """
        if value in (MovementReason.SALE, MovementReason.REFUND):
            raise serializers.ValidationError(
                "Sales and refunds move stock through a sale, not through a "
                "manual adjustment."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        delta = attrs.get("delta")
        new_quantity = attrs.get("new_quantity")

        if (delta is None) == (new_quantity is None):
            raise serializers.ValidationError(
                {"detail": "Give either a delta or a new quantity, not both and not neither."}
            )
        if delta is not None and delta == 0:
            raise serializers.ValidationError({"delta": "A change of zero does nothing."})

        # Enforced here as well as in apply_movement, so the caller gets a
        # field-level error rather than a 500 from a service-layer ValueError.
        if attrs["reason"] in REASONS_REQUIRING_NOTE and not attrs.get("note", "").strip():
            raise serializers.ValidationError(
                {
                    "note": (
                        "A reason is required for this kind of change. Stock "
                        "that moves without an explanation cannot be "
                        "reconciled later."
                    )
                }
            )
        return attrs


class StockMovementSerializer(serializers.ModelSerializer):
    """One entry in the ledger."""

    item_name = serializers.CharField(source="stock_item.item.name", read_only=True)
    store_code = serializers.CharField(source="stock_item.store.code", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True, default=None)
    reason_label = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = (
            "id",
            "stock_item",
            "item_name",
            "store_code",
            "delta",
            "balance_after",
            "reason",
            "reason_label",
            "note",
            "ref_type",
            "ref_id",
            "user",
            "user_name",
            "created_at",
        )
        read_only_fields = fields

    def get_reason_label(self, obj: StockMovement) -> str:
        return (
            MovementReason(obj.reason).label
            if obj.reason in MovementReason.values
            else obj.reason
        )
