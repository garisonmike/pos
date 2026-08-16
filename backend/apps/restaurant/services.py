"""
Everything that changes an order.

The module's whole job is the gap between food going out and money coming in.
At the end of that gap, :func:`bill_order` hands the order to the ordinary
checkout and this module stops being involved - the sale, its receipt, its
compliance document, its shift attribution and its reporting all happen exactly
as they do for a duka.

Two rules are worth stating before the code.

**Nothing here prices anything.** An order is handed to ``create_sale`` as an
ordinary cart and priced by ``apps.sales.pricing``, so discount, tax and
rounding have one implementation rather than two. A priced modifier bills as
its own catalogue line rather than altering a dish's unit price, because
``create_sale`` deliberately ignores a client-supplied price - see
:func:`bill_order`.

**Voiding after the kitchen has cooked is an authority question.** Before a
ticket prints, a void costs nothing. After one, the restaurant has already spent
the ingredients and the void makes that disappear - which is the same shape as
a discount, and goes through the same mechanism.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Item
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.restaurant.models import (
    ALLOWED_ORDER_TRANSITIONS,
    KitchenTicket,
    Modifier,
    ModifierGroup,
    Order,
    OrderLine,
    OrderLineModifier,
    OrderState,
    Table,
)
from apps.sales.authorization import DiscountNotAuthorized, resolve_discount_authorization
from apps.sales.services import LineRequest, create_sale
from apps.tenants.models import ModuleKey


class OrderError(Exception):
    """Something a waiter can act on, rather than a bug."""

    def __init__(self, detail: str, code: str = "order_error"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def require_module(tenant) -> None:
    """Refuse a business that has not switched the module on.

    Checked in the service rather than only at the view, because the module
    boundary is a data question - a duka must not acquire tables because
    somebody found the endpoint.
    """
    if not tenant.has_module(ModuleKey.RESTAURANT):
        raise OrderError(
            "This business does not have the restaurant module switched on.",
            "module_not_enabled",
        )


def assert_can_transition(current: str, target: str) -> None:
    if target not in ALLOWED_ORDER_TRANSITIONS.get(current, frozenset()):
        raise OrderError(
            f"An order that is {current.lower()} cannot become {target.lower()}.",
            "illegal_transition",
        )


@transaction.atomic
def open_order(
    *, tenant, store, table, user, covers: int = 0, note: str = "", request=None
) -> Order:
    """Start an order against a table.

    The one-live-order-per-table constraint does the real work; this reports it
    in words a waiter can act on rather than letting an IntegrityError surface.
    """
    require_module(tenant)

    if table is not None and not table.is_active:
        raise OrderError("That table is not in use.", "table_inactive")

    existing = Order.objects.filter(
        tenant=tenant, table=table, state__in=(OrderState.OPEN, OrderState.SENT)
    ).first()
    if existing is not None:
        raise OrderError(
            "That table already has an order open. Add to it, or bill it first.",
            "table_occupied",
        )

    order = Order.objects.create(
        tenant=tenant,
        store=store,
        table=table,
        opened_by=user,
        opened_at=timezone.now(),
        covers=covers,
        note=note,
    )
    record_audit(
        action=AuditAction.CREATE,
        entity=order,
        actor=user,
        request=request,
        after={"table": table.name if table else None, "covers": covers},
    )
    return order


def _validate_modifier_choices(item: Item, modifiers: list[Modifier]) -> None:
    """Check a line answers the questions the kitchen needs answered.

    A steak with no doneness chosen is an order nobody can cook, and finding
    that out at the pass is worse than finding it out at the table.
    """
    chosen_by_group: dict[str, int] = {}
    for modifier in modifiers:
        chosen_by_group[str(modifier.group_id)] = (
            chosen_by_group.get(str(modifier.group_id), 0) + 1
        )

    for group in item.modifier_groups.all():
        count = chosen_by_group.get(str(group.id), 0)
        if count < group.min_choices:
            raise OrderError(
                f"{group.name} needs at least {group.min_choices} choice"
                f"{'' if group.min_choices == 1 else 's'}.",
                "modifier_required",
            )
        if group.max_choices and count > group.max_choices:
            raise OrderError(
                f"{group.name} allows at most {group.max_choices}.",
                "too_many_modifiers",
            )


@transaction.atomic
def add_line(
    *,
    order: Order,
    item_id: str,
    quantity: Decimal,
    modifier_ids: list[str] | None = None,
    note: str = "",
    user=None,
    request=None,
) -> OrderLine:
    """Put something on an order.

    Allowed while an order is OPEN *or* SENT - a table ordering pudding after
    the mains have gone out is the ordinary case, not an exception. What it
    produces is a line with no ticket yet, which is exactly how the next ticket
    knows what to carry.
    """
    if not order.is_live:
        raise OrderError(
            "That order is closed and cannot be added to.", "order_not_live"
        )
    if quantity <= 0:
        raise OrderError("A quantity must be more than zero.", "bad_quantity")

    item = Item.objects.filter(pk=item_id, is_active=True).first()
    if item is None:
        raise OrderError("No such item.", "item_not_found")
    if not item.is_available:
        raise OrderError(f"{item.name} is not available.", "item_unavailable")

    modifiers = list(Modifier.objects.filter(pk__in=modifier_ids or [], is_active=True))
    if len(modifiers) != len(modifier_ids or []):
        raise OrderError("One of those choices is no longer offered.", "modifier_not_found")

    _validate_modifier_choices(item, modifiers)

    line = OrderLine.objects.create(
        tenant=order.tenant,
        order=order,
        item=item,
        name=item.till_label or item.name,
        unit=item.unit,
        base_price_cents=item.price_cents,
        quantity=quantity,
        note=note,
    )

    for modifier in modifiers:
        # Frozen onto the line. Renaming "extra chilli" next week must not
        # restate what this table was charged tonight.
        OrderLineModifier.objects.create(
            tenant=order.tenant,
            order_line=line,
            modifier=modifier,
            name=modifier.name,
            price_cents=modifier.price_cents,
            item=modifier.item,
        )

    return line


@transaction.atomic
def void_line(*, line: OrderLine, user, reason: str, request=None) -> OrderLine:
    """Strike a line off before payment.

    Kept rather than deleted. A line that reached the kitchen and was then
    cancelled is a thing somebody may have to explain, and deleting it removes
    the only evidence the conversation happened.
    """
    if not line.order.is_live:
        raise OrderError("That order is closed.", "order_not_live")
    if line.is_voided:
        return line
    if not reason.strip():
        raise OrderError("Striking a line off needs a reason.", "reason_required")

    line.is_voided = True
    line.void_reason = reason
    line.save(update_fields=["is_voided", "void_reason", "updated_at"])

    record_audit(
        action=AuditAction.VOID,
        entity=line,
        actor=user,
        request=request,
        reason=reason,
        after={
            "order": str(line.order_id),
            "item": line.name,
            "quantity": str(line.quantity),
            # Whether the kitchen had already been told is the fact that
            # matters when somebody reads this back.
            "had_been_sent": line.first_ticket_id is not None,
        },
    )
    return line


@transaction.atomic
def send_to_kitchen(*, order: Order, user=None, request=None) -> KitchenTicket:
    """Print a ticket for whatever is new.

    **Only what has not been sent before.** A waiter adding two drinks mid-meal
    must not have the kitchen cook the whole table again - that is a real and
    expensive failure, not a tidiness concern. Lines carry the ticket that
    first covered them, so "new" is a fact rather than a guess about timing.
    """
    if not order.is_live:
        raise OrderError("That order is closed.", "order_not_live")

    pending = list(order.lines.filter(first_ticket__isnull=True, is_voided=False))
    if not pending:
        raise OrderError(
            "Nothing new to send. Add something first.", "nothing_to_send"
        )

    sequence = (
        order.tickets.order_by("-sequence").values_list("sequence", flat=True).first() or 0
    ) + 1

    ticket = KitchenTicket.objects.create(
        tenant=order.tenant,
        order=order,
        sequence=sequence,
        printed_by=user,
        printed_at=timezone.now(),
    )
    OrderLine.objects.filter(pk__in=[line.pk for line in pending]).update(
        first_ticket=ticket
    )

    if order.state == OrderState.OPEN:
        assert_can_transition(order.state, OrderState.SENT)
        order.state = OrderState.SENT
        order.save(update_fields=["state", "updated_at"])

    return ticket


def reprint_ticket(*, ticket: KitchenTicket) -> KitchenTicket:
    """Print a ticket again, exactly as it was.

    Not "everything new now" - that is what ``send_to_kitchen`` is for. A
    reprint of ticket two is ticket two, or the kitchen ends up with a
    different set of food from the one the waiter asked for.
    """
    ticket.reprint_count += 1
    ticket.save(update_fields=["reprint_count", "updated_at"])
    return ticket


def void_order(
    *,
    order: Order,
    user,
    reason: str,
    payload: dict | None = None,
    request=None,
) -> Order:
    """Cancel a whole order.

    **Authority is required once the kitchen has been told.** Before a ticket
    prints, nothing has been cooked and a void costs the restaurant nothing.
    After one, the ingredients are spent and the void makes that disappear -
    which is the same shape as a discount, and goes through the very same
    mechanism: a manager or owner authorises from their own session, anybody
    else needs a manager's credential verified right now.

    The lines that had already been sent are recorded on the audit entry,
    because "what had the kitchen already made" is the first question anybody
    asks afterwards.
    """
    if not order.is_live:
        raise OrderError("That order is already closed.", "order_not_live")
    if not reason.strip():
        raise OrderError("A void needs a reason.", "reason_required")

    already_sent = list(order.lines.filter(first_ticket__isnull=False, is_voided=False))

    # Authority is settled **outside** the transaction below, deliberately.
    # A refusal writes an audit entry, and that entry is the whole point of
    # refusing - somebody standing at a till working through a manager's four
    # digits must leave a trace. Resolving this inside the atomic block would
    # roll the entry back along with the failed void, which is precisely
    # backwards.
    if already_sent:
        try:
            authorization = resolve_discount_authorization(
                actor=user,
                payload={**(payload or {}), "reason": reason},
                request=request,
                refused_action=AuditAction.ORDER_VOID_REFUSED,
            )
        except DiscountNotAuthorized as exc:
            raise OrderError(exc.detail, exc.code) from exc
        authorised_by = authorization.label
        via = authorization.via
    else:
        authorised_by = user.username
        via = "NOT_REQUIRED"

    with transaction.atomic():
        return _apply_void(
            order=order,
            user=user,
            reason=reason,
            authorised_by=authorised_by,
            via=via,
            already_sent=already_sent,
            request=request,
        )


def _apply_void(*, order, user, reason, authorised_by, via, already_sent, request):
    """The state change itself, once authority is settled."""
    assert_can_transition(order.state, OrderState.VOID)
    order.state = OrderState.VOID
    order.void_reason = reason
    order.closed_at = timezone.now()
    order.save(update_fields=["state", "void_reason", "closed_at", "updated_at"])

    record_audit(
        action=AuditAction.VOID,
        entity=order,
        actor=user,
        request=request,
        reason=reason,
        after={
            "table": order.table.name if order.table else None,
            "authorized_by": authorised_by,
            "via": via,
            # The whole point of recording this: somebody has to know what the
            # kitchen had already made.
            "lines_already_sent": [
                {"item": line.name, "quantity": str(line.quantity)}
                for line in already_sent
            ],
            "sent_line_count": len(already_sent),
        },
    )
    return order


@transaction.atomic
def move_order(*, order: Order, table: Table, user, request=None) -> Order:
    """Move an order to a different table.

    Recorded rather than silently rewritten. "Table four moved to the terrace
    at eight" is a question a manager asks when a bill goes to the wrong party.
    """
    if not order.is_live:
        raise OrderError("That order is closed.", "order_not_live")
    if not table.is_active:
        raise OrderError("That table is not in use.", "table_inactive")

    occupied = (
        Order.objects.filter(
            tenant=order.tenant, table=table, state__in=(OrderState.OPEN, OrderState.SENT)
        )
        .exclude(pk=order.pk)
        .exists()
    )
    if occupied:
        raise OrderError(
            "That table already has an order open. Merge them instead.",
            "table_occupied",
        )

    before = order.table.name if order.table else None
    order.table = table
    order.save(update_fields=["table", "updated_at"])

    record_audit(
        action=AuditAction.UPDATE,
        entity=order,
        actor=user,
        request=request,
        before={"table": before},
        after={"table": table.name},
    )
    return order


@transaction.atomic
def merge_orders(*, source: Order, target: Order, user, request=None) -> Order:
    """Fold one table's order into another's.

    The emptied order is left in ``MERGED`` rather than deleted, so that where
    its lines went stays answerable. Deleting it would make a bill somebody
    queried simply not exist.
    """
    if source.pk == target.pk:
        raise OrderError("An order cannot be merged into itself.", "same_order")
    if not source.is_live or not target.is_live:
        raise OrderError("Both orders must still be open.", "order_not_live")

    moved = list(source.lines.all())
    OrderLine.objects.filter(order=source).update(order=target)

    assert_can_transition(source.state, OrderState.MERGED)
    source.state = OrderState.MERGED
    source.merged_into = target
    source.table = None
    source.closed_at = timezone.now()
    source.save(
        update_fields=["state", "merged_into", "table", "closed_at", "updated_at"]
    )

    record_audit(
        action=AuditAction.UPDATE,
        entity=source,
        actor=user,
        request=request,
        reason="Merged",
        after={
            "merged_into": str(target.id),
            "target_table": target.table.name if target.table else None,
            "lines_moved": len(moved),
        },
    )
    return target


@transaction.atomic
def bill_order(*, order: Order, user, request=None):
    """Turn an order into an ordinary sale.

    After this the restaurant module is done with it: the sale settles, prints,
    is invoiced, lands in a shift and appears in reports through exactly the
    same code a duka uses.

    **A priced modifier bills as its own line.** ``create_sale`` prices from
    the catalogue and ignores a client-supplied price for anything not marked
    variable - that guard is what stops a till selling at whatever it likes,
    and this module is not going to weaken it to fold a surcharge into a dish
    price. So "extra chilli" is a catalogue item, and a steak with extra chilli
    becomes two lines. The customer can read what they were charged for, which
    is better than an invisible surcharge.

    A free modifier produces no line at all. The kitchen was told; the till has
    nothing to charge for.
    """
    if not order.is_live:
        raise OrderError("That order is already closed.", "order_not_live")

    lines = list(order.lines.filter(is_voided=False).prefetch_related("modifiers"))
    if not lines:
        raise OrderError("There is nothing on that order.", "empty_order")

    requests: list[LineRequest] = []
    for line in lines:
        requests.append(
            LineRequest(item_id=str(line.item_id), quantity=line.quantity)
        )
        for modifier in line.modifiers.all():
            if modifier.price_cents <= 0 or modifier.item_id is None:
                continue
            # One modifier line per unit of the dish: two steaks with extra
            # chilli is two chillies, not one.
            requests.append(
                LineRequest(
                    item_id=str(modifier.item_id), quantity=line.quantity
                )
            )

    sale = create_sale(
        tenant=order.tenant,
        store=order.store,
        cashier=user,
        lines=requests,
        note=order.note,
    )

    order.sale = sale
    order.save(update_fields=["sale", "updated_at"])
    return sale


@transaction.atomic
def close_order(*, order: Order, user=None, request=None) -> Order:
    """Mark an order paid, once its sale has settled."""
    assert_can_transition(order.state, OrderState.BILLED)
    order.state = OrderState.BILLED
    order.closed_at = timezone.now()
    order.save(update_fields=["state", "closed_at", "updated_at"])
    return order


def modifier_surcharge_cents(order: Order) -> int:
    """What the modifiers on an order come to.

    Reported separately so a restaurant can see what extras are earning, and
    kept out of the item price for the reason in ``bill_order``.
    """
    total = 0
    for line in order.lines.filter(is_voided=False).prefetch_related("modifiers"):
        per_unit = sum(modifier.price_cents for modifier in line.modifiers.all())
        total += int(per_unit * line.quantity)
    return total


def open_orders_for(tenant, *, store=None):
    """Every table currently owing money."""
    queryset = Order.objects.filter(
        tenant=tenant, state__in=(OrderState.OPEN, OrderState.SENT)
    ).select_related("table", "opened_by")
    if store is not None:
        queryset = queryset.filter(store=store)
    return queryset.order_by("opened_at")


def groups_for_item(item: Item):
    """The questions a kitchen needs answered about one item."""
    return ModifierGroup.objects.filter(items=item).prefetch_related("modifiers")
