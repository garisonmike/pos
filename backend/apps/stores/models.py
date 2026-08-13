"""
Branches.

Every business starts with exactly one store, created by the setup wizard, and
most will never have a second. The model exists anyway because the alternative
- hanging stock quantities off the item itself - is the change that cannot be
made later without rewriting every stock query and migrating live data.

The seam is specifically this: an item's *identity* (name, price, barcode) is
business-wide, while its *quantity* is per store. That split is what makes a
second branch an insert rather than a redesign.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel


class Store(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """One physical location belonging to a business."""

    name = models.CharField(max_length=120)
    code = models.CharField(
        max_length=20,
        help_text="Short label used on receipts and reports, for example 'MAIN'.",
    )
    phone = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text=(
            "Where sales are recorded when a till has not been told otherwise. "
            "Exactly one per business."
        ),
    )

    class Meta:
        db_table = "stores_store"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="unique_store_code_per_tenant"
            ),
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_default=True),
                name="one_default_store_per_tenant",
            ),
        ]

    def __str__(self) -> str:
        return self.name
