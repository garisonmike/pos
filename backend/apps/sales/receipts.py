"""
Receipt numbering.

Sequential per business and **gapless**, because this is the series tax
reporting reads. A gap invites the question of what was removed from it, and
that is not a question a shop owner should have to answer about a number the
software allocated.

Gapless means the number must be allocated inside the same transaction that
commits the sale. A number handed out first and committed later leaves a hole
whenever the sale rolls back. So a counter row is locked, incremented and
committed alongside the sale: if the sale fails, the increment rolls back with
it and the number is reused.

The cost is one lock per completed sale per business. At the volume a duka does
- a few hundred sales a day - that is nothing. It would matter for a chain
selling thousands an hour, and at that point the answer is a counter per store
rather than abandoning gaplessness.

A till with no connection cannot take part in this. It prints a device-scoped
provisional reference instead, and the business-wide number is allocated when
the sale syncs; both stay on the record so the paper in a customer's hand can be
matched to the fiscal number afterwards.
"""

from __future__ import annotations

from django.db import transaction

from apps.sales.models import ReceiptCounter


def allocate_receipt_number(tenant, *, when=None) -> tuple[int, str]:
    """Take the next number in a business's series.

    **Must be called inside the transaction that commits the sale.** Called
    outside one, the increment commits on its own and a failed sale leaves a
    permanent gap - which is the one thing this function exists to prevent.

    ``select_for_update`` serialises concurrent tills. Two cashiers completing
    sales at the same instant queue rather than both reading the same last
    number and issuing it twice; the unique constraint would catch that, but as
    a failed sale rather than as an orderly wait.
    """
    from django.utils import timezone

    when = when or timezone.now()

    counter, _created = ReceiptCounter.objects.select_for_update().get_or_create(
        tenant=tenant, defaults={"last_number": 0}
    )
    counter.last_number += 1
    counter.save(update_fields=["last_number", "updated_at"])

    number = counter.last_number
    code = f"{counter.prefix}-{when.year}-{number:06d}"
    return number, code


def provisional_reference(device_name: str, sequence: int) -> str:
    """The reference a disconnected till prints.

    Device-scoped, so two tills cannot produce the same one, and visibly not a
    fiscal number - it carries the till's name rather than the business's
    invoice prefix, so nobody mistakes it for the series tax reporting reads.
    """
    slug = "".join(ch for ch in device_name.upper() if ch.isalnum())[:8] or "TILL"
    return f"{slug}-{sequence:06d}"


@transaction.atomic
def peek_next_number(tenant) -> int:
    """What the next number would be, without taking it.

    For a settings screen showing where a business's series has got to. Never
    used to allocate: reading and then writing separately is exactly the race
    the locked allocation above avoids.
    """
    counter = ReceiptCounter.objects.filter(tenant=tenant).first()
    return (counter.last_number if counter else 0) + 1
