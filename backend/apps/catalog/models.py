"""
What a business sells.

The central decision here is that there is one ``Item`` model covering both a
tin of beans and a haircut, rather than separate models per business type.
A service is an item that is not stock-tracked and has a duration; a product is
an item that is stock-tracked and has a barcode. Everything downstream - the
cart, the sale line, tax, discounts, receipts, reports - then works on items
without caring which kind it holds, which is what stops a restaurant build from
becoming a fork of the retail build.

Anything genuinely specific to one business type lives in that module's own
tables and points back here: batch and expiry for pharmacies, duration and
staff assignment for salons, modifiers for restaurants. A duka's database
therefore carries none of those columns.

All money is stored as an integer number of cents. See ``apps.core.money`` for
why, and for the arithmetic every other app is expected to use.
"""

from __future__ import annotations

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel


class ItemType(models.TextChoices):
    """Whether this is a thing on a shelf or work performed."""

    PRODUCT = "PRODUCT", "Physical product"
    SERVICE = "SERVICE", "Service"


class UnitOfMeasure(models.TextChoices):
    """How an item is counted at the till.

    Loose goods matter here. Sugar and flour are sold by weight from an open
    sack, so quantities have to be fractional; that is why stock quantities are
    Decimal rather than integer throughout.
    """

    EACH = "EACH", "Each"
    KILOGRAM = "KG", "Kilogram"
    GRAM = "G", "Gram"
    LITRE = "L", "Litre"
    MILLILITRE = "ML", "Millilitre"
    METRE = "M", "Metre"
    HOUR = "HOUR", "Hour"


class Category(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """A grouping used for navigation at the till and for reporting.

    Self-referencing so "Drinks" can contain "Sodas" without a second model.
    Depth is not enforced in the database; the till UI shows two levels, and a
    deeper tree would simply be awkward to tap through rather than incorrect.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=110)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        null=True,
        blank=True,
    )
    display_order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers appear first on the till's category buttons."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "catalog_category"
        ordering = ("display_order", "name")
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "slug"], name="unique_category_slug_per_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.name


class TaxRate(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """A tax rate, and crucially whether prices carrying it already include it.

    The rate is stored in basis points - 16% is 1600 - so that the entire tax
    calculation stays in integer arithmetic and no rate can arrive as a float
    that is almost but not exactly 16%.

    ``is_inclusive`` is the field that decides how a price is interpreted, and
    it lives here rather than on the tenant so that a single business can sell
    VAT-inclusive goods over the counter and quote VAT-exclusive prices to a
    trade customer. The tenant's ``vat_mode`` only supplies the default when a
    new rate is created.
    """

    name = models.CharField(max_length=60, help_text="As it appears on a receipt, e.g. 'VAT 16%'.")
    rate_bps = models.PositiveIntegerField(
        validators=[MinValueValidator(0)],
        help_text="Basis points. 1600 is 16%, 0 is zero-rated.",
    )
    is_inclusive = models.BooleanField(
        default=True,
        help_text=(
            "True when the marked price already contains this tax, which is "
            "normal for Kenyan retail: the customer pays exactly the shelf price."
        ),
    )
    is_default = models.BooleanField(
        default=False, help_text="Applied to new items when none is chosen."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "catalog_tax_rate"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="unique_tax_rate_name_per_tenant"
            ),
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_default=True),
                name="one_default_tax_rate_per_tenant",
            ),
        ]

    def __str__(self) -> str:
        basis = "incl" if self.is_inclusive else "excl"
        return f"{self.name} ({self.rate_bps / 100:g}%, {basis})"

    @property
    def rate_percent(self) -> float:
        """The rate as a percentage, for display only. Never used in arithmetic."""
        return self.rate_bps / 100


class Item(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """Something a business sells: a product on a shelf or a service performed.

    One model, not two, and the reason is worth stating because it is the thing
    every later module depends on. A service is an item that is not
    stock-tracked and has a duration. A product is an item that is stock-tracked
    and has barcodes. Everything downstream - cart, sale line, tax, discounts,
    receipts, reports - operates on *items*, so none of it needs a second code
    path when the restaurant, salon and pharmacy modules arrive.

    Fields that only one kind of business uses still live here rather than in a
    product-only table, as long as every kind could plausibly want them. A salon
    needs "fully booked today" exactly as much as a duka needs "out of stock",
    and quoting a price on the day is as normal for braiding as it is for
    damaged retail stock. Splitting those out would be the retail assumption
    that makes services second-class.

    What does *not* belong here is anything stock-shaped beyond the
    ``track_stock`` switch. Quantities live in ``inventory.StockItem``, per
    store, so a services-only business has no rows in that table at all and no
    dead columns here.
    """

    sku = models.CharField(
        max_length=64,
        help_text="The business's own code for this item. Unique within the business.",
    )
    name = models.CharField(max_length=150)
    short_name = models.CharField(
        max_length=24,
        blank=True,
        help_text=(
            "Label for till buttons and receipt lines, where the full name will "
            "not fit. Falls back to a truncated name when blank."
        ),
    )
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="items",
        null=True,
        blank=True,
    )

    item_type = models.CharField(
        max_length=10, choices=ItemType.choices, default=ItemType.PRODUCT
    )
    unit = models.CharField(
        max_length=6, choices=UnitOfMeasure.choices, default=UnitOfMeasure.EACH
    )

    price_cents = models.BigIntegerField(
        validators=[MinValueValidator(0)],
        help_text=(
            "Selling price in cents. Whether tax is included is decided by the "
            "tax rate. When is_price_variable is set this is a suggestion "
            "rather than the price charged."
        ),
    )
    is_price_variable = models.BooleanField(
        default=False,
        help_text=(
            "The cashier enters the price at the till, starting from the "
            "suggestion above. For services quoted on the day and for retail "
            "oddments such as damaged stock."
        ),
    )
    cost_cents = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text=(
            "What the business paid, in cents. Used for margin reporting, "
            "never shown to customers."
        ),
    )
    tax_rate = models.ForeignKey(
        TaxRate,
        on_delete=models.PROTECT,
        related_name="items",
        null=True,
        blank=True,
        help_text="Null means not taxed.",
    )

    track_stock = models.BooleanField(
        default=True,
        help_text=(
            "Off for services, and for prepared food that is made to order "
            "rather than counted. A business can mix both freely."
        ),
    )
    duration_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How long a service takes. Used for appointment booking; null for products.",
    )

    image = models.ImageField(upload_to="items/", blank=True, null=True)
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text="Lower numbers appear first on the till. Ties fall back to name.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Delisted. An inactive item is gone from the till entirely and stays "
            "only so past sales still resolve."
        ),
    )
    is_available = models.BooleanField(
        default=True,
        help_text=(
            "Temporarily off-sale, and deliberately separate from is_active. "
            "'Out of season', 'kitchen has run out' and 'stylist off today' are "
            "everyday states that must not require delisting an item and "
            "re-creating it."
        ),
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        related_name="created_items",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "catalog_item"
        ordering = ("sort_order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "sku"], name="unique_item_sku_per_tenant"
            ),
            # A service has no shelf to count, so tracking stock on one is
            # always a mistake rather than a preference.
            models.CheckConstraint(
                condition=~models.Q(item_type=ItemType.SERVICE, track_stock=True),
                name="services_do_not_track_stock",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "name"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.sku})"

    @property
    def is_service(self) -> bool:
        return self.item_type == ItemType.SERVICE

    @property
    def till_label(self) -> str:
        """What a till button shows: the short name, or a trimmed full name."""
        return self.short_name or self.name[:24]

    @property
    def is_sellable(self) -> bool:
        """Whether this can be added to a sale right now.

        Both flags, because they mean different things: ``is_active`` is
        delisted for good, ``is_available`` is off today. Stock is deliberately
        not consulted here - a shop may sell the last bag while the count says
        zero, and refusing that at the catalogue layer would put the cashier in
        an argument with the customer over a number.
        """
        return self.is_active and self.is_available


class Barcode(TenantOwnedModel, TimeStampedModel):
    """A scannable code pointing at an item.

    A separate table rather than a field on the item, because the same product
    genuinely arrives with different codes: a supplier changes packaging, a
    multipack carries its own code, or a shop prints its own labels for loose
    goods. Modelling it as one field per item forces a shop to choose which
    barcode "counts", and the one they did not choose then fails to scan.

    Codes are unique within a business rather than globally, since two
    unrelated shops printing their own labels can easily collide.
    """

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="barcodes")
    code = models.CharField(max_length=64, db_index=True)
    is_primary = models.BooleanField(
        default=False, help_text="The code printed on this shop's own shelf labels."
    )

    class Meta:
        db_table = "catalog_barcode"
        ordering = ("-is_primary", "code")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="unique_barcode_per_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.code
