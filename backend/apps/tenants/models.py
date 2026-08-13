"""
The tenant: one business using the platform.

``Tenant`` is deliberately *not* a ``TenantOwnedModel`` and carries no
Row-Level Security policy. It is the registry that isolation is defined
against, so isolating it against itself would be circular - the login endpoint
has to read a tenant by slug before it knows which tenant to bind. Access to
this table is controlled at the application layer instead: only the platform
surfaces list or create tenants, and a tenant's own users can read exactly one
row, their own.

Everything else in this app, and in every other app, does carry a tenant and is
protected by the database policy.
"""

from __future__ import annotations

from django.db import models
from django.utils.text import slugify

from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel


class BusinessType(models.TextChoices):
    """What kind of business this is.

    This selects a template at setup time - a set of sensible defaults and a
    starting set of enabled modules - and is not a hard constraint afterwards.
    A salon that starts selling shampoo can switch stock tracking on without
    changing its business type, because the type only ever seeded the defaults.
    """

    RETAIL = "RETAIL", "Retail shop"
    RESTAURANT = "RESTAURANT", "Restaurant or bar"
    SALON = "SALON", "Salon or services"
    PHARMACY = "PHARMACY", "Pharmacy"


class TenantStatus(models.TextChoices):
    """Whether this business may currently trade.

    There is no "deleted" state on purpose. A business that stops paying keeps
    its sales history, because that history is a legal record its owner may
    need years later, and because a cancelled customer who returns should not
    have to start again.
    """

    TRIAL = "TRIAL", "On trial"
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    CANCELLED = "CANCELLED", "Cancelled"


class VatMode(models.TextChoices):
    """How this business's prices relate to tax.

    ``PER_ITEM`` is the general case and the other two are conveniences: the
    real decision is always the ``is_inclusive`` flag on the tax rate attached
    to an item. Recording a mode here lets the setup wizard and the catalogue
    UI default sensibly instead of asking about tax on every single product.
    """

    INCLUSIVE = "INCLUSIVE", "Prices include tax"
    EXCLUSIVE = "EXCLUSIVE", "Tax added at checkout"
    PER_ITEM = "PER_ITEM", "Decided per item"


class ModuleKey(models.TextChoices):
    """Optional capabilities a tenant can have switched on.

    Modules are additive: each one owns its own tables and reads the shared
    tenant, item and sale models without altering them. That is what keeps a
    restaurant's table management from appearing as dead columns in a duka's
    database, and what lets a new business type be added without a fork.
    """

    STOCK = "stock", "Stock tracking"
    MPESA = "mpesa", "M-Pesa payments"
    COMPLIANCE = "compliance", "Tax compliance and invoicing"
    RESTAURANT = "restaurant", "Tables, orders and modifiers"
    APPOINTMENTS = "appointments", "Appointment booking"
    PHARMACY_BATCHES = "pharmacy_batches", "Batch and expiry tracking"


class Tenant(UUIDModel, TimeStampedModel):
    """One business on the platform."""

    name = models.CharField(max_length=150)
    slug = models.SlugField(
        max_length=60,
        unique=True,
        help_text=(
            "Short identifier the till sends at sign-in. Entered once when a "
            "device is set up and remembered from then on, which is why "
            "usernames only need to be unique within a business."
        ),
    )
    business_type = models.CharField(
        max_length=20, choices=BusinessType.choices, default=BusinessType.RETAIL
    )
    status = models.CharField(
        max_length=20,
        choices=TenantStatus.choices,
        default=TenantStatus.TRIAL,
        db_index=True,
    )

    # Locale. Currency is stored per tenant rather than assumed globally so
    # that a future non-Kenyan customer does not require a schema change,
    # though only KES is exercised today.
    country = models.CharField(max_length=2, default="KE")
    currency = models.CharField(max_length=3, default="KES")
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")

    vat_mode = models.CharField(
        max_length=20, choices=VatMode.choices, default=VatMode.INCLUSIVE
    )
    kra_pin = models.CharField(
        max_length=20,
        blank=True,
        help_text="Tax PIN printed on receipts and required for tax invoices.",
    )

    # Contact and receipt branding.
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    logo = models.FileField(upload_to="tenant-logos/", blank=True, null=True)
    receipt_header = models.CharField(max_length=200, blank=True)
    receipt_footer = models.CharField(
        max_length=200, blank=True, default="Thank you for your business"
    )

    trial_ends_at = models.DateTimeField(null=True, blank=True)
    setup_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the owner finishes the setup wizard. Guards it from being re-run.",
    )
    notes = models.TextField(
        blank=True, help_text="Platform operator's notes. Never shown to the tenant."
    )

    class Meta:
        db_table = "tenants_tenant"
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    def save(self, *args, **kwargs):
        """Derive a slug from the business name when one was not supplied."""
        if not self.slug:
            self.slug = slugify(self.name)[:60]
        super().save(*args, **kwargs)

    @property
    def is_operational(self) -> bool:
        """Whether tills belonging to this business may currently trade."""
        return self.status in (TenantStatus.ACTIVE, TenantStatus.TRIAL)

    @property
    def is_setup_complete(self) -> bool:
        return self.setup_completed_at is not None

    def has_module(self, key: str) -> bool:
        """Whether an optional capability is switched on for this business."""
        return self.modules.filter(module_key=key, is_enabled=True).exists()


class TenantModule(TenantOwnedModel, TimeStampedModel):
    """One optional capability, switched on or off for one business.

    A table rather than a list field on ``Tenant`` for two reasons. It carries
    per-module configuration, which a flag cannot. And it is queryable across
    tenants, so the platform operator can answer "who is using the restaurant
    module" - which is exactly the question billing needs to ask.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="modules"
    )
    module_key = models.CharField(max_length=32, choices=ModuleKey.choices)
    is_enabled = models.BooleanField(default=False)
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Module-specific settings. Kept as JSON because each module owns "
            "its own shape and validates it in its own serializer."
        ),
    )

    class Meta:
        db_table = "tenants_tenant_module"
        ordering = ("module_key",)
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "module_key"], name="unique_module_per_tenant"
            )
        ]

    def __str__(self) -> str:
        state = "on" if self.is_enabled else "off"
        return f"{self.module_key} ({state})"
