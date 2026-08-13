"""
Stock: how much of each item is at each branch, and how it got that way.

Two models, and the split between them is the important part.

``StockItem`` holds the current quantity. It exists per item **per store**,
which is the seam that makes a second branch an insert rather than a redesign -
an item's identity is business-wide, its quantity is not.

``StockMovement`` is an append-only ledger. Every change to a quantity writes
one, and ``StockItem.quantity`` is a cached running total that can be rebuilt
from the ledger at any time. Keeping both looks redundant until the first time a
count disagrees with the shelf: the cached number answers "how many", and only
the ledger answers "why", which is the question a shop owner actually asks.

Quantities are ``Decimal``, not integers, because loose goods are real. Sugar
and flour come out of an open sack by weight, so 2.5 kg has to be expressible.
Money remains integer cents everywhere; see ``apps.core.money``.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models, transaction

from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel

#: Three decimal places covers grams within a kilogram, which is as fine as any
#: counter scale in a duka reads.
QUANTITY_DECIMAL_PLACES = 3
QUANTITY_MAX_DIGITS = 12


class MovementReason(models.TextChoices):
    """Why stock moved, in terms a shop owner would use.

    The reason is not decoration. It separates shrinkage from sales from
    miscounts, which is the whole point of keeping a ledger rather than
    overwriting a number.
    """

    PURCHASE = "PURCHASE", "Stock received"
    RETURN = "RETURN", "Customer return"
    ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"
    WASTAGE = "WASTAGE", "Damaged, expired or lost"
    COUNT = "COUNT", "Stock count correction"
    TRANSFER_IN = "TRANSFER_IN", "Transferred in from another branch"
    TRANSFER_OUT = "TRANSFER_OUT", "Transferred out to another branch"
    SALE = "SALE", "Sold"
    REFUND = "REFUND", "Refunded"


#: Reasons a person enters by hand, and which therefore require an explanation.
#: Sales and refunds are excluded: those carry a sale reference instead, which
#: says more than free text ever would.
REASONS_REQUIRING_NOTE = frozenset(
    {MovementReason.ADJUSTMENT, MovementReason.WASTAGE, MovementReason.COUNT}
)


class StockItem(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """How much of one item is held at one branch."""

    item = models.ForeignKey(
        "catalog.Item", on_delete=models.PROTECT, related_name="stock_levels"
    )
    store = models.ForeignKey(
        "stores.Store", on_delete=models.PROTECT, related_name="stock_levels"
    )

    quantity = models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=(
            "Current quantity. A cached running total of this item's movements, "
            "rebuildable from them if it is ever doubted."
        ),
    )
    reorder_level = models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=(
            "Warn when the quantity reaches this. Held per branch rather than "
            "per item, because a kiosk and a depot reorder at different points."
        ),
    )
    last_counted_at = models.DateTimeField(
        null=True, blank=True, help_text="When someone last physically counted this."
    )

    class Meta:
        db_table = "inventory_stock_item"
        ordering = ("item__name",)
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "item", "store"], name="unique_stock_per_item_store"
            )
        ]
        indexes = [models.Index(fields=["tenant", "store"])]

    def __str__(self) -> str:
        return f"{self.item.name} @ {self.store.code}: {self.quantity}"

    @property
    def is_low(self) -> bool:
        """Whether this has reached the point where someone should reorder.

        A reorder level of zero means "do not warn me", not "warn me always" -
        otherwise every item a shop has not configured would sit permanently in
        the alerts list and the list would be ignored.
        """
        return self.reorder_level > 0 and self.quantity <= self.reorder_level

    @property
    def is_negative(self) -> bool:
        """Stock below zero, which always means the records are wrong somewhere.

        Allowed rather than prevented: a sale that has already happened must be
        recordable even when the count disagrees. It is surfaced instead of
        blocked, so a manager can reconcile it.
        """
        return self.quantity < 0


class StockMovement(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """One change to one stock level. Append-only; never edited or deleted.

    Corrections are new movements in the opposite direction, exactly as a
    ledger works, so the history of what was believed and when survives. An
    editable movement would be worth very little as an audit record.
    """

    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="movements"
    )
    delta = models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        help_text="Signed change. Negative takes stock away.",
    )
    balance_after = models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        help_text=(
            "Quantity immediately after this movement. Stored so history reads "
            "without replaying the whole ledger, and so a later disagreement "
            "can be traced to the movement that introduced it."
        ),
    )
    reason = models.CharField(max_length=16, choices=MovementReason.choices)
    note = models.TextField(
        blank=True,
        help_text="Why. Required for adjustments, wastage and count corrections.",
    )

    # Links to whatever caused this, without a foreign key per source. Sales
    # arrive in milestone 3 and imports already exist, so a rigid FK here would
    # need changing every time a new cause appears.
    ref_type = models.CharField(max_length=32, blank=True)
    ref_id = models.CharField(max_length=64, blank=True)

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        related_name="stock_movements",
        null=True,
        blank=True,
        help_text="Who made the change. Null only for automated corrections.",
    )

    class Meta:
        db_table = "inventory_stock_movement"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["stock_item", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.reason} {self.delta:+} -> {self.balance_after}"


@transaction.atomic
def apply_movement(
    *,
    stock_item: StockItem,
    delta: Decimal,
    reason: str,
    user=None,
    note: str = "",
    ref_type: str = "",
    ref_id: str = "",
) -> StockMovement:
    """Change a stock level and record why, as one indivisible step.

    The row is locked for the duration. Two cashiers selling the last of
    something at the same moment would otherwise both read the same quantity and
    write back totals that each ignore the other, losing a movement - the
    classic lost update, and one that shows up as stock that will not reconcile
    rather than as an error anyone notices.

    Negative results are allowed deliberately. Refusing them would mean refusing
    to record a sale that has already happened, which puts the books further
    from the truth than a negative number does. Callers surface the condition
    instead; see ``StockItem.is_negative``.
    """
    if reason in REASONS_REQUIRING_NOTE and not note.strip():
        raise ValueError(f"A note is required for {reason} movements.")

    locked = StockItem.objects.select_for_update().get(pk=stock_item.pk)
    new_quantity = locked.quantity + delta

    locked.quantity = new_quantity
    locked.save(update_fields=["quantity", "updated_at"])

    return StockMovement.objects.create(
        tenant_id=locked.tenant_id,
        stock_item=locked,
        delta=delta,
        balance_after=new_quantity,
        reason=reason,
        note=note,
        ref_type=ref_type,
        ref_id=ref_id,
        user=user,
    )


def rebuild_quantity(stock_item: StockItem) -> Decimal:
    """Recompute a quantity from its movements and write it back.

    The ledger is the source of truth; the quantity on ``StockItem`` is a cache
    of it. This exists so that a disagreement between the two can always be
    resolved in favour of the record that explains itself.
    """
    total = stock_item.movements.aggregate(total=models.Sum("delta"))["total"] or Decimal("0")
    StockItem.objects.filter(pk=stock_item.pk).update(quantity=total)
    return total
