"""
Tables, the orders held against them, and what the kitchen is told.

**Additive, not a fork.** A restaurant sells the same ``Item`` rows through the
same checkout as a duka. Nothing in ``apps.sales`` changes shape for this
module; these tables point back at the shared models and a retail business
touches none of them.

The one thing that genuinely differs from retail is the *order of events*. A
duka takes money and hands over goods in one motion; a restaurant sends food out
and collects payment afterwards. Everything here exists in that gap, and stops
existing the moment it closes:

    Order (OPEN -> SENT -> BILLED) --> becomes an ordinary Sale

**An open order is not revenue.** It has no receipt number, no invoice number,
moves no stock, and appears in no report. The reporting layer filters on
``Sale.state`` and never sees an order at all, which is correct and is why it
needed no change.

**Stock moves at settlement, not when food leaves the kitchen.** Depleting
ingredients as a dish is cooked needs recipe modelling, which is a milestone of
its own. Recorded here so the limitation is a decision rather than a surprise.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel


class Table(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """A table, as the staff refer to it.

    A name, not a coordinate. Floor plans and reservations are deliberately out
    of scope: a waiter needs to know which table an order belongs to, and a
    picture of the room does not help them do that faster.
    """

    store = models.ForeignKey(
        "stores.Store", on_delete=models.PROTECT, related_name="tables"
    )
    name = models.CharField(
        max_length=40, help_text="How staff call it. 'Table 4', 'Terrace 2', 'Bar'."
    )
    seats = models.PositiveIntegerField(
        default=0, help_text="Informational. Nothing refuses a party of five on a four-top."
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Tables are deactivated rather than deleted, so the orders that "
            "were served at one keep a name attached to them."
        ),
    )

    class Meta:
        db_table = "restaurant_table"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "store", "name"], name="unique_table_name_per_store"
            )
        ]
        indexes = [models.Index(fields=["tenant", "store", "is_active"])]

    def __str__(self) -> str:
        return self.name


class OrderState(models.TextChoices):
    """Where an order has got to.

    ``SENT`` is the state that matters. It means food has left the kitchen, and
    it is the line either side of which a void means something completely
    different: before, nobody has cooked anything; after, the restaurant has
    already spent the ingredients.
    """

    OPEN = "OPEN", "Being taken"
    SENT = "SENT", "Sent to the kitchen"
    BILLED = "BILLED", "Paid and closed"
    VOID = "VOID", "Cancelled"
    MERGED = "MERGED", "Merged into another table"


#: What a person may do to an order.
#:
#: BILLED and VOID are terminal, and MERGED is terminal for the emptied order -
#: it survives as a record that the merge happened rather than being deleted,
#: so "table 4 went into table 6 at eight" stays answerable.
ALLOWED_ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    # OPEN goes straight to BILLED as well as through SENT. A drink poured at
    # the bar and paid for on the spot never reaches a kitchen, and requiring a
    # ticket for it would make the commonest bar sale impossible.
    OrderState.OPEN: frozenset(
        {OrderState.SENT, OrderState.BILLED, OrderState.VOID, OrderState.MERGED}
    ),
    OrderState.SENT: frozenset({OrderState.BILLED, OrderState.VOID, OrderState.MERGED}),
    OrderState.BILLED: frozenset(),
    OrderState.VOID: frozenset(),
    OrderState.MERGED: frozenset(),
}


class Order(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """What a table has asked for, before anybody has paid.

    Deliberately **not** a ``Sale``. It is the thing that becomes one. Keeping
    them separate is what stops an unpaid order appearing in revenue, and what
    lets the sale machinery stay exactly as it is for a duka that has no tables.
    """

    store = models.ForeignKey(
        "stores.Store", on_delete=models.PROTECT, related_name="orders"
    )
    table = models.ForeignKey(
        Table,
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
        help_text="Null once merged away, so the emptied order keeps its history.",
    )
    state = models.CharField(
        max_length=8, choices=OrderState.choices, default=OrderState.OPEN
    )

    opened_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="opened_orders"
    )
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    #: The sale this became, once somebody paid. Null until then, and the only
    #: link between this module and the money.
    sale = models.OneToOneField(
        "sales.Sale",
        on_delete=models.PROTECT,
        related_name="restaurant_order",
        null=True,
        blank=True,
    )

    #: Where an order went when its table was merged into another.
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="merged_from",
        null=True,
        blank=True,
    )

    covers = models.PositiveIntegerField(
        default=0, help_text="How many people are eating. For per-head reporting later."
    )
    note = models.TextField(blank=True)
    void_reason = models.TextField(blank=True)

    class Meta:
        db_table = "restaurant_order"
        ordering = ("-opened_at",)
        constraints = [
            # One live order per table. Two would make "what does table four
            # owe" unanswerable, and that question is the entire point of the
            # record.
            models.UniqueConstraint(
                fields=["tenant", "table"],
                condition=models.Q(state__in=("OPEN", "SENT")),
                name="one_live_order_per_table",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state", "-opened_at"]),
            models.Index(fields=["tenant", "table"]),
        ]

    def __str__(self) -> str:
        return f"{self.table_id or 'no table'} ({self.state})"

    @property
    def is_live(self) -> bool:
        return self.state in (OrderState.OPEN, OrderState.SENT)

    @property
    def has_been_sent(self) -> bool:
        """Whether the kitchen has been told about any of it.

        Read from the ticket ledger rather than from the state, because an
        order can go back to being added to after a ticket has printed - and
        what matters for a void is whether anything was *cooked*, not what the
        state column happens to say now.
        """
        return self.tickets.exists()


class OrderLine(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """One thing a table asked for.

    Snapshots its name and price the same way a ``SaleLine`` does, and for the
    same reason: a price change during service must not rewrite what a table
    was quoted an hour ago.
    """

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(
        "catalog.Item", on_delete=models.PROTECT, related_name="order_lines"
    )

    name = models.CharField(max_length=150)
    unit = models.CharField(max_length=6)
    #: The catalogue price at the moment it was ordered, before modifiers.
    base_price_cents = models.BigIntegerField()
    quantity = models.DecimalField(max_digits=12, decimal_places=3)

    note = models.TextField(
        blank=True, help_text="Free text for the kitchen. 'Well done', 'allergy - nuts'."
    )

    #: Which kitchen ticket first carried this line. Null until one does, which
    #: is exactly how "what is new since the last ticket" is answered.
    first_ticket = models.ForeignKey(
        "restaurant.KitchenTicket",
        on_delete=models.SET_NULL,
        related_name="lines",
        null=True,
        blank=True,
    )

    is_voided = models.BooleanField(
        default=False,
        help_text=(
            "A line struck off before payment. Kept rather than deleted, "
            "because a line that reached the kitchen and was then cancelled is "
            "a thing somebody may need to explain."
        ),
    )
    void_reason = models.TextField(blank=True)

    class Meta:
        db_table = "restaurant_order_line"
        ordering = ("created_at",)
        indexes = [models.Index(fields=["tenant", "order"])]

    def __str__(self) -> str:
        return f"{self.quantity} x {self.name}"

    @property
    def unit_price_cents(self) -> int:
        """What one of these costs, modifiers included.

        **For showing a waiter a running total, not for billing.** The bill is
        produced by ``bill_order``, which sends the dish and each priced
        modifier to the catalogue as separate lines - because ``create_sale``
        ignores a client-supplied price, and that guard is what stops a till
        selling at whatever it likes.

        The two agree on the figure. They differ in who is trusted to compute
        it, and only the server's answer reaches a receipt.
        """
        return self.base_price_cents + sum(
            modifier.price_cents for modifier in self.modifiers.all()
        )


class ModifierGroup(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """A question the kitchen needs answered about an item.

    "How would you like it cooked" is one choice from several; "any extras" is
    any number from several. The bounds are held here and checked when a line
    is added, because a steak with no doneness is an order the kitchen cannot
    act on.
    """

    name = models.CharField(max_length=80)
    items = models.ManyToManyField(
        "catalog.Item", related_name="modifier_groups", blank=True
    )

    min_choices = models.PositiveIntegerField(
        default=0, help_text="0 makes the group optional."
    )
    max_choices = models.PositiveIntegerField(
        default=0, help_text="0 means no limit."
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "restaurant_modifier_group"
        ordering = ("position", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="unique_modifier_group_per_tenant"
            ),
            models.CheckConstraint(
                condition=models.Q(max_choices=0)
                | models.Q(max_choices__gte=models.F("min_choices")),
                name="modifier_max_not_below_min",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_required(self) -> bool:
        return self.min_choices > 0


class Modifier(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """One answer. Sometimes it costs something, often it does not."""

    group = models.ForeignKey(
        ModifierGroup, on_delete=models.CASCADE, related_name="modifiers"
    )
    name = models.CharField(max_length=80)
    price_cents = models.BigIntegerField(
        default=0,
        help_text=(
            "What it adds. Zero for 'no onions', which the kitchen still needs "
            "told about even though the till does not care."
        ),
    )
    #: What a priced modifier is actually sold as.
    #:
    #: A modifier that costs money must point at a catalogue item, because
    #: ``create_sale`` prices from the catalogue and ignores a client-supplied
    #: price for anything not marked variable. That guard is what stops a till
    #: selling at whatever it likes, and this module is not going to weaken it.
    #:
    #: So "extra chilli" is an ordinary Item the restaurant creates once, and a
    #: steak with extra chilli bills as two lines. The customer can read what
    #: they were charged for, which is better than a surcharge folded invisibly
    #: into a dish price.
    item = models.ForeignKey(
        "catalog.Item",
        on_delete=models.PROTECT,
        related_name="sold_as_modifier",
        null=True,
        blank=True,
        help_text="Required when price_cents is above zero.",
    )
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "restaurant_modifier"
        ordering = ("position", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["group", "name"], name="unique_modifier_per_group"
            ),
            # A price with nothing to sell it as would be charged to nobody.
            models.CheckConstraint(
                condition=models.Q(price_cents=0) | models.Q(item__isnull=False),
                name="priced_modifier_has_an_item",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class OrderLineModifier(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """A modifier as it was chosen, frozen.

    Carries its own name and price rather than reading through to ``Modifier``,
    so that renaming "extra chilli" or repricing it next week does not restate
    what a table was charged tonight - the same rule ``SaleLine`` follows.
    """

    order_line = models.ForeignKey(
        OrderLine, on_delete=models.CASCADE, related_name="modifiers"
    )
    modifier = models.ForeignKey(
        Modifier, on_delete=models.PROTECT, related_name="chosen", null=True, blank=True
    )
    name = models.CharField(max_length=80)
    price_cents = models.BigIntegerField(default=0)
    #: The catalogue item a priced modifier bills as, frozen alongside the
    #: price so a later reconfiguration cannot restate tonight's bill.
    item = models.ForeignKey(
        "catalog.Item",
        on_delete=models.PROTECT,
        related_name="billed_as_modifier",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "restaurant_order_line_modifier"
        ordering = ("created_at",)

    def __str__(self) -> str:
        return self.name


class KitchenTicket(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """What the kitchen was told, and when.

    Append-only, numbered per order. **A ticket carries only what was added
    since the last one.** Reprinting the whole order when a waiter adds two
    drinks mid-meal would have the kitchen cook everything twice, which is a
    real and expensive failure rather than a tidiness concern.

    Reprinting a ticket reprints *that* ticket, not "everything new now".
    """

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="tickets")
    sequence = models.PositiveIntegerField(
        help_text="1, 2, 3 within one order. Printed so the kitchen can spot a gap."
    )
    printed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        related_name="kitchen_tickets",
        null=True,
        blank=True,
    )
    printed_at = models.DateTimeField()
    reprint_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            "How many times this was printed again. A kitchen that keeps asking "
            "for reprints is telling you something about the printer."
        ),
    )

    class Meta:
        db_table = "restaurant_kitchen_ticket"
        ordering = ("order", "sequence")
        constraints = [
            models.UniqueConstraint(
                fields=["order", "sequence"], name="unique_ticket_sequence_per_order"
            ),
        ]
        indexes = [models.Index(fields=["tenant", "-printed_at"])]

    def __str__(self) -> str:
        return f"Ticket {self.sequence} for {self.order_id}"
