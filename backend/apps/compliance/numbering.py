"""
Invoice numbering.

Gapless per business, for the same reason receipt numbering is: this is a series
a revenue authority reads, and a gap invites the question of what was removed
from it.

Gapless means the number is allocated **inside the transaction that commits the
sale**, never before it and never afterwards. A number handed out first and
committed later leaves a hole whenever the sale rolls back.

There is deliberately no deferred or queued allocation path. An online sale gets
its number in its own transaction, immediately. An offline sale gets one when it
syncs - which is the same code, running later, because syncing *is* when that
sale commits. What must never exist is a mechanism that defers allocation for a
sale that has already committed: that would put the hole back.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.compliance.models import InvoiceCounter


def allocate_invoice_number(tenant, *, when=None) -> tuple[int, str]:
    """Take the next number in a business's tax-invoice series.

    **Must be called inside the transaction that commits the document.** Called
    outside one, the increment commits on its own and a failed sale leaves a
    permanent gap - the one thing this function exists to prevent.

    ``select_for_update`` serialises concurrent tills, so two cashiers settling
    at the same instant queue rather than both reading the same last number.
    The unique constraint would catch a collision anyway, but as a failed sale
    rather than as an orderly wait.
    """
    when = when or timezone.now()

    counter, _created = InvoiceCounter.objects.select_for_update().get_or_create(
        tenant=tenant, defaults={"last_number": 0}
    )
    counter.last_number += 1
    counter.save(update_fields=["last_number", "updated_at"])

    number = counter.last_number
    code = f"{counter.prefix}-{when.year}-{number:06d}"
    return number, code


@transaction.atomic
def peek_next_invoice_number(tenant) -> int:
    """What the next number would be, without taking it.

    For a settings screen showing where a business's series has reached. Never
    used to allocate: reading and then writing separately is exactly the race
    the locked allocation above avoids.
    """
    counter = InvoiceCounter.objects.filter(tenant=tenant).first()
    return (counter.last_number if counter else 0) + 1
