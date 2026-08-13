"""
Serializers for the catalogue.

Two rules run through all of them. The business is always taken from the
authenticated user and never from the payload, so a request cannot create or
move a row into another business - the database would refuse it anyway, but
failing here gives a usable error instead of a constraint violation. And any
field naming another record is validated against the caller's own business, so
pointing at another business's category comes back as "not found" rather than
as a foreign key error.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.catalog.models import Barcode, Category, Item, ItemType, TaxRate, UnitOfMeasure
from apps.core.money import from_cents, split_exclusive, split_inclusive


class CategorySerializer(serializers.ModelSerializer):
    """A grouping of items, one or two levels deep."""

    item_count = serializers.IntegerField(read_only=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)

    class Meta:
        model = Category
        fields = (
            "id",
            "name",
            "slug",
            "parent",
            "parent_name",
            "display_order",
            "is_active",
            "item_count",
            "created_at",
        )
        read_only_fields = ("id", "created_at", "item_count", "parent_name")
        extra_kwargs = {"slug": {"required": False}}

    def validate_parent(self, value: Category | None) -> Category | None:
        """A category may only sit under one from the same business.

        Also refuses a category as its own parent. Deeper cycles are not
        possible through this endpoint because the tree is two levels in the
        till, but the direct case is easy to hit by mistake when editing.
        """
        if value is None:
            return value

        tenant = self.context["request"].user.tenant
        if value.tenant_id != tenant.id:
            raise serializers.ValidationError("No such category.")
        if self.instance and value.pk == self.instance.pk:
            raise serializers.ValidationError("A category cannot be its own parent.")
        return value

    def validate_name(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs: dict) -> dict:
        """Derive a slug from the name, and keep it unique within the business."""
        from django.utils.text import slugify

        tenant = self.context["request"].user.tenant
        name = attrs.get("name") or (self.instance.name if self.instance else "")

        if not attrs.get("slug"):
            attrs["slug"] = slugify(name)[:110] or "category"

        clashes = Category.objects.filter(tenant=tenant, slug=attrs["slug"])
        if self.instance:
            clashes = clashes.exclude(pk=self.instance.pk)
        if clashes.exists():
            raise serializers.ValidationError(
                {"name": "Another category in this business already uses that name."}
            )
        return attrs


class TaxRateSerializer(serializers.ModelSerializer):
    """A tax rate, and whether prices carrying it already include it."""

    rate_percent = serializers.FloatField(read_only=True)
    item_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TaxRate
        fields = (
            "id",
            "name",
            "rate_bps",
            "rate_percent",
            "is_inclusive",
            "is_default",
            "is_active",
            "item_count",
            "created_at",
        )
        read_only_fields = ("id", "created_at", "rate_percent", "item_count")

    def validate_rate_bps(self, value: int) -> int:
        """Basis points, so 16% is 1600.

        Capped at 100%. A rate above that is always a unit mistake - someone
        entering 16 as 160000 - and letting it through would mis-price every
        item attached to it.
        """
        if value < 0:
            raise serializers.ValidationError("A tax rate cannot be negative.")
        if value > 10_000:
            raise serializers.ValidationError(
                "That is over 100%. Rates are in basis points: 16% is 1600."
            )
        return value

    def validate_name(self, value: str) -> str:
        tenant = self.context["request"].user.tenant
        clashes = TaxRate.objects.filter(tenant=tenant, name=value.strip())
        if self.instance:
            clashes = clashes.exclude(pk=self.instance.pk)
        if clashes.exists():
            raise serializers.ValidationError(
                "Another tax rate in this business already uses that name."
            )
        return value.strip()


class BarcodeSerializer(serializers.ModelSerializer):
    """One scannable code. An item may have several."""

    class Meta:
        model = Barcode
        fields = ("id", "code", "is_primary", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_code(self, value: str) -> str:
        """Codes are unique within a business, not globally.

        Two unrelated shops printing their own labels will collide eventually,
        and neither is wrong, so uniqueness stops at the business boundary.
        """
        code = value.strip()
        if not code:
            raise serializers.ValidationError("A barcode cannot be blank.")

        tenant = self.context["request"].user.tenant
        clash = Barcode.objects.filter(tenant=tenant, code=code)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)

        existing = clash.select_related("item").first()
        if existing is not None:
            raise serializers.ValidationError(
                f"That barcode already belongs to {existing.item.name}."
            )
        return code


class TaxBreakdownSerializer(serializers.Serializer):
    """How one unit of an item splits between net and tax.

    Present on every item so the till can show a VAT line without repeating the
    arithmetic, and so the inclusive/exclusive distinction is visible in the API
    rather than implied. All values are integer cents; ``*_display`` fields are
    the same amounts formatted for a receipt.
    """

    net_cents = serializers.IntegerField()
    tax_cents = serializers.IntegerField()
    gross_cents = serializers.IntegerField()
    rate_bps = serializers.IntegerField()
    is_inclusive = serializers.BooleanField()
    gross_display = serializers.CharField()


def tax_breakdown_for(item: Item) -> dict:
    """Split an item's price into net and tax according to its rate.

    Which direction to split is decided by the rate's ``is_inclusive`` flag, not
    by anything on the business, which is what lets one business sell
    VAT-inclusive over the counter and quote VAT-exclusive to trade.

    With no rate attached the item is untaxed and the price is entirely net.
    """
    price = item.price_cents

    if item.tax_rate_id is None or not item.tax_rate.is_active:
        return {
            "net_cents": price,
            "tax_cents": 0,
            "gross_cents": price,
            "rate_bps": 0,
            "is_inclusive": True,
            "gross_display": f"{from_cents(price):,.2f}",
        }

    rate = item.tax_rate
    if rate.is_inclusive:
        net, tax = split_inclusive(price, rate.rate_bps)
        gross = price
    else:
        gross, tax = split_exclusive(price, rate.rate_bps)
        net = price

    return {
        "net_cents": net,
        "tax_cents": tax,
        "gross_cents": gross,
        "rate_bps": rate.rate_bps,
        "is_inclusive": rate.is_inclusive,
        "gross_display": f"{from_cents(gross):,.2f}",
    }


class ItemSerializer(serializers.ModelSerializer):
    """An item as the till reads it."""

    barcodes = BarcodeSerializer(many=True, read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    tax_rate_name = serializers.CharField(source="tax_rate.name", read_only=True, default=None)
    till_label = serializers.CharField(read_only=True)
    is_sellable = serializers.BooleanField(read_only=True)
    tax_breakdown = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = (
            "id",
            "sku",
            "name",
            "short_name",
            "till_label",
            "description",
            "category",
            "category_name",
            "item_type",
            "unit",
            "price_cents",
            "is_price_variable",
            "cost_cents",
            "tax_rate",
            "tax_rate_name",
            "tax_breakdown",
            "track_stock",
            "duration_minutes",
            "image",
            "sort_order",
            "is_active",
            "is_available",
            "is_sellable",
            "barcodes",
            "stock",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def get_tax_breakdown(self, obj: Item) -> dict:
        return tax_breakdown_for(obj)

    def get_stock(self, obj: Item) -> list[dict]:
        """Quantity per branch, or an empty list for anything untracked.

        Services and made-to-order food have no stock at all, and returning an
        empty list rather than a zero keeps "not tracked" distinct from "none
        left" - a distinction the till has to draw before deciding whether to
        warn a cashier.
        """
        if not obj.track_stock:
            return []
        levels = getattr(obj, "prefetched_stock", None)
        if levels is None:
            levels = obj.stock_levels.select_related("store").all()
        return [
            {
                "store_id": str(level.store_id),
                "store_code": level.store.code,
                "quantity": str(level.quantity),
                "reorder_level": str(level.reorder_level),
                "is_low": level.is_low,
            }
            for level in levels
        ]


class ItemWriteSerializer(serializers.ModelSerializer):
    """Creating and editing an item, with its barcodes in the same request.

    Barcodes are accepted inline because a shop adding a product scans its code
    at the same moment - making that a second request would mean an item can
    exist briefly with no way to scan it.
    """

    barcodes = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        help_text="Scannable codes. The first becomes the primary one.",
    )

    class Meta:
        model = Item
        fields = (
            "id",
            "sku",
            "name",
            "short_name",
            "description",
            "category",
            "item_type",
            "unit",
            "price_cents",
            "is_price_variable",
            "cost_cents",
            "tax_rate",
            "track_stock",
            "duration_minutes",
            "image",
            "sort_order",
            "is_active",
            "is_available",
            "barcodes",
        )
        read_only_fields = ("id",)

    def validate_sku(self, value: str) -> str:
        sku = value.strip()
        if not sku:
            raise serializers.ValidationError("An SKU is required.")

        tenant = self.context["request"].user.tenant
        clashes = Item.objects.filter(tenant=tenant, sku=sku)
        if self.instance:
            clashes = clashes.exclude(pk=self.instance.pk)
        if clashes.exists():
            raise serializers.ValidationError(
                "Another item in this business already uses that SKU."
            )
        return sku

    def validate_category(self, value: Category | None) -> Category | None:
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("No such category.")
        return value

    def validate_tax_rate(self, value: TaxRate | None) -> TaxRate | None:
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("No such tax rate.")
        return value

    def validate_barcodes(self, value: list[str]) -> list[str]:
        """Codes must be unique within the business and within the request."""
        codes = [code.strip() for code in value if code.strip()]

        duplicates = {code for code in codes if codes.count(code) > 1}
        if duplicates:
            raise serializers.ValidationError(
                f"Repeated in this request: {', '.join(sorted(duplicates))}."
            )

        tenant = self.context["request"].user.tenant
        taken = Barcode.objects.filter(tenant=tenant, code__in=codes)
        if self.instance:
            taken = taken.exclude(item=self.instance)

        clash = taken.select_related("item").first()
        if clash is not None:
            raise serializers.ValidationError(
                f"{clash.code} already belongs to {clash.item.name}."
            )
        return codes

    def validate(self, attrs: dict) -> dict:
        """Cross-field rules that keep services and products both coherent."""
        item_type = attrs.get(
            "item_type", self.instance.item_type if self.instance else ItemType.PRODUCT
        )
        track_stock = attrs.get(
            "track_stock", self.instance.track_stock if self.instance else True
        )
        unit = attrs.get("unit", self.instance.unit if self.instance else UnitOfMeasure.EACH)

        if item_type == ItemType.SERVICE:
            if track_stock:
                raise serializers.ValidationError(
                    {
                        "track_stock": (
                            "A service has no shelf to count. Turn stock "
                            "tracking off, or make this a product."
                        )
                    }
                )
            # Not an error: a service billed by the hour is normal, and so is
            # one sold as a flat job.
            attrs.setdefault("duration_minutes", attrs.get("duration_minutes"))

        if item_type == ItemType.PRODUCT and unit == UnitOfMeasure.HOUR:
            raise serializers.ValidationError(
                {"unit": "Hours are a unit for services, not for products."}
            )

        return attrs

    def create(self, validated_data: dict) -> Item:
        codes = validated_data.pop("barcodes", [])
        request = self.context["request"]

        item = Item.objects.create(
            tenant=request.user.tenant, created_by=request.user, **validated_data
        )
        self._sync_barcodes(item, codes)
        return item

    def update(self, instance: Item, validated_data: dict) -> Item:
        codes = validated_data.pop("barcodes", None)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if codes is not None:
            self._sync_barcodes(instance, codes, replace=True)
        return instance

    @staticmethod
    def _sync_barcodes(item: Item, codes: list[str], *, replace: bool = False) -> None:
        """Attach codes to an item, first one primary.

        On update the set is replaced rather than merged, so removing a code
        from the list removes it from the item - which is what an editor showing
        the full list implies.
        """
        if replace:
            item.barcodes.exclude(code__in=codes).delete()

        existing = set(item.barcodes.values_list("code", flat=True))
        for index, code in enumerate(codes):
            if code in existing:
                item.barcodes.filter(code=code).update(is_primary=index == 0)
                continue
            Barcode.objects.create(
                tenant=item.tenant, item=item, code=code, is_primary=index == 0
            )
