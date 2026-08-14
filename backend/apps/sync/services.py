"""
Replaying a till's backlog onto the server.

Three things make this different from the online checkout, and each of them is
a way money goes wrong if it is handled casually.

**A replay is normal, not exceptional.** The connection that came back is the
same connection that drops again mid-upload. So every sale is keyed on
``client_uuid`` and the second arrival of one is a *success* that creates
nothing - never an error, and never a second sale.

**Idempotency is the database's job, not a lookup's.** The obvious shape - look
for an existing sale, create one if absent - is a race with a window between
the two statements, and two threads uploading the same batch will both find
nothing and both insert. So this inserts first and treats ``IntegrityError`` on
``unique_sale_client_uuid_per_tenant`` as the duplicate signal. The constraint
is the arbiter because the constraint is the only thing that is actually atomic.

**The till's arithmetic is evidence, not authority.** The server prices every
cart again from its own catalogue. A disagreement is recorded as a discrepancy
and the sale still lands, because the goods have already left the shop; what a
disagreement usually means is a till carrying a stale price list, and that is
worth a person's attention rather than a rejection nobody sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction

from apps.accounts.constants import UserRole
from apps.accounts.models import Device, User
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.sales.authorization import AuthorizationMethod
from apps.sales.models import Sale, SaleDiscrepancy
from apps.sales.services import CheckoutError, LineRequest, create_sale, take_cash


@dataclass
class SaleOutcome:
    """What happened to one sale in a batch.

    Returned per sale rather than rolled into one status for the request,
    because the till has to know exactly which rows it may delete from its
    outbox. A batch-level failure would make it delete all of them or none.
    """

    client_uuid: str
    status: str
    sale_id: str | None = None
    receipt_number: int | None = None
    detail: str = ""
    code: str = ""
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "client_uuid": self.client_uuid,
            "status": self.status,
            "sale_id": self.sale_id,
            "receipt_number": self.receipt_number,
            "detail": self.detail,
            "code": self.code,
            "flags": self.flags,
        }


#: Accepted and written for the first time.
ACCEPTED = "accepted"
#: Already present. Nothing was written; the till may drop it from its outbox.
DUPLICATE = "duplicate"
#: Not written, and retrying unchanged will not help. The till must keep it and
#: surface it to a person rather than silently discarding money.
REJECTED = "rejected"


def resolve_device(*, tenant, device_id) -> Device | None:
    """Find the till a batch claims to come from, within this business only.

    The query is tenant-scoped, so a device id belonging to another business
    simply is not found - there is no cross-tenant comparison to get wrong and
    no branch that could accidentally accept one. This is the reason the check
    is written as a lookup rather than as ``device.tenant_id == tenant.id``:
    the latter needs the row first, and fetching the row is the mistake.
    """
    return Device.objects.filter(pk=device_id, is_active=True).first()


def check_pin_version(*, tenant, username: str, claimed_version: int) -> tuple[User | None, str]:
    """Compare the PIN version a device authorised against with the current one.

    Returns the manager, if the name resolves inside this business, and a
    string naming what is wrong - empty when nothing is.

    **Be precise about what this establishes.** It reliably catches a device
    approving against a cached PIN that has since been changed or revoked: the
    server bumps ``pin_version`` on every ``set_pin`` and ``clear_pin``, so a
    till that was offline across either of those events reports a number that no
    longer matches, and the sale is flagged.

    It does **not** prove the device performed the check. Anyone who can edit
    the till's local database can also read the version that database holds, so
    a fabricated payload built on a current cache will carry a matching version.
    Detecting that would need the device to hold a secret the fabricator cannot
    read, which a rooted Android tablet does not offer. The claim here is
    therefore the narrow one - staleness and revocation, not forgery - and the
    genuine defence against a tampered till remains revoking it.
    """
    manager = User.objects.filter(tenant=tenant, username=username).first()

    if manager is None:
        return None, "unknown_user"
    if not manager.is_active:
        return manager, "inactive_user"
    if not manager.has_role_at_least(UserRole.MANAGER):
        return manager, "insufficient_role"
    if manager.pin_version != claimed_version:
        return manager, "stale_pin_version"
    if not manager.pin_hash:
        return manager, "no_pin_set"
    return manager, ""


@transaction.atomic
def replay_sale(*, tenant, store, cashier, device, payload: dict, request=None) -> SaleOutcome:
    """Write one offline sale, or report why it is already here.

    Wrapped in its own transaction so that a sale which fails partway takes its
    lines, its payment and its stock movements with it, and leaves the rest of
    the batch alone.
    """
    client_uuid = str(payload["client_uuid"])

    lines = [
        LineRequest(
            item_id=str(line["item_id"]),
            quantity=Decimal(str(line["quantity"])),
            unit_price_cents=line.get("unit_price_cents"),
            discount_bps=line.get("discount_bps", 0),
            discount_cents=line.get("discount_cents", 0),
        )
        for line in payload["lines"]
    ]

    authorization = payload.get("discount_authorization")
    flags: list[str] = []

    try:
        # Insert first. The unique constraint on (tenant, client_uuid) is what
        # decides whether this is a first arrival, and it is checked inside the
        # database rather than by a preceding SELECT that another thread could
        # slip past.
        with transaction.atomic():
            sale = create_sale(
                tenant=tenant,
                store=store,
                cashier=cashier,
                lines=lines,
                device=device,
                cart_discount_bps=payload.get("cart_discount_bps", 0),
                cart_discount_cents=payload.get("cart_discount_cents", 0),
                client_uuid=client_uuid,
                customer_phone=payload.get("customer_phone", ""),
                note=payload.get("note", ""),
            )
    except IntegrityError:
        # Someone got here first - an earlier batch, or a concurrent copy of
        # this one. Both are successes. The savepoint above has already been
        # rolled back, so this transaction is usable again.
        existing = Sale.objects.filter(tenant=tenant, client_uuid=client_uuid).first()
        if existing is None:
            # A different constraint failed, which is a bug rather than a
            # replay. Surfacing it beats reporting a duplicate that is not one.
            raise
        return SaleOutcome(
            client_uuid=client_uuid,
            status=DUPLICATE,
            sale_id=str(existing.id),
            receipt_number=existing.receipt_number,
            detail="Already recorded.",
        )
    except CheckoutError as exc:
        return SaleOutcome(
            client_uuid=client_uuid,
            status=REJECTED,
            detail=exc.detail,
            code=exc.code,
        )

    sale.was_offline = True
    sale.device_created_at = payload["device_created_at"]
    sale.device_sequence = payload.get("device_sequence")

    if authorization is not None:
        flags.extend(
            _apply_offline_authorization(
                tenant=tenant,
                sale=sale,
                cashier=cashier,
                authorization=authorization,
                request=request,
            )
        )

    sale.save()

    _flag_totals_mismatch(sale=sale, claimed=payload.get("total_cents"), flags=flags)

    try:
        take_cash(
            sale=sale,
            tendered_cents=payload["tendered_cents"],
            user=cashier,
            round_to_shilling=payload.get("round_to_shilling", True),
        )
    except CheckoutError as exc:
        # The sale exists and the goods are gone, so this cannot roll back the
        # whole batch - but it must roll back this sale, or the shop is left
        # with an unpayable OPEN row nobody will ever close.
        raise CheckoutError(exc.detail, exc.code) from exc

    sale.refresh_from_db()
    return SaleOutcome(
        client_uuid=client_uuid,
        status=ACCEPTED,
        sale_id=str(sale.id),
        receipt_number=sale.receipt_number,
        flags=flags,
    )


def _apply_offline_authorization(*, tenant, sale, cashier, authorization, request) -> list[str]:
    """Record a discount the till approved with no connection.

    The sale is never rejected for a failed comparison. By the time this runs
    the customer has walked out with the goods, and refusing the record would
    delete the evidence rather than the problem.
    """
    username = authorization["username"]
    claimed_version = authorization["pin_version"]
    manager, problem = check_pin_version(
        tenant=tenant, username=username, claimed_version=claimed_version
    )

    sale.discount_authorized_by = manager
    sale.discount_authorized_label = username
    sale.discount_authorization_reason = authorization["reason"]
    sale.discount_authorized_via = AuthorizationMethod.OFFLINE
    sale.discount_authorized_at = authorization["authorized_at"]
    sale.discount_authorized_pin_version = claimed_version
    sale.discount_authorization_is_stale = bool(problem)

    if not problem:
        return []

    SaleDiscrepancy.objects.create(
        tenant=tenant,
        sale=sale,
        kind=SaleDiscrepancy.Kind.STALE_AUTHORIZATION,
        detail=(
            f"Offline discount on this sale was approved against {username}'s "
            f"cached PIN, which no longer matches the one held now ({problem})."
        ),
        context={
            "problem": problem,
            "attempted_authorizer": username,
            "claimed_pin_version": claimed_version,
            "current_pin_version": manager.pin_version if manager else None,
            "acting_cashier": cashier.username,
            "discount_cents": sale.discount_cents,
        },
    )
    record_audit(
        action=AuditAction.DISCOUNT_REFUSED,
        entity=sale,
        actor=cashier,
        request=request,
        reason=problem,
        after={"attempted_authorizer": username, "claimed_pin_version": claimed_version},
    )
    return ["stale_authorization"]


def _flag_totals_mismatch(*, sale, claimed, flags: list[str]) -> None:
    """Record that the till and the server priced the same cart differently.

    Almost always a till holding a price list from before the last change. The
    server's figure stands - it is the one priced from the current catalogue -
    and the disagreement is surfaced so somebody notices the stale till.
    """
    if claimed is None or claimed == sale.total_cents:
        return

    SaleDiscrepancy.objects.create(
        tenant=sale.tenant,
        sale=sale,
        kind=SaleDiscrepancy.Kind.TOTALS_MISMATCH,
        detail=(
            f"The till made this {claimed} cents; the server prices it at "
            f"{sale.total_cents} cents. The server's figure stands."
        ),
        context={
            "device_total_cents": claimed,
            "server_total_cents": sale.total_cents,
            "difference_cents": claimed - sale.total_cents,
        },
    )
    flags.append("totals_mismatch")


def record_offline_refusals(*, cashier, device, refusals: list[dict], request=None) -> int:
    """Write the authorisation attempts the till turned down while offline.

    These are the entries that would otherwise exist only on the tablet. A shop
    owner needs to be able to see that somebody spent an evening trying four
    digits at a disconnected till, and that is only true if the attempts come
    back when it reconnects.

    Filed against the attempted username as a bare string with no user foreign
    key, exactly as an online refusal and a failed sign-in are: nobody proved
    they were that person, and attaching the manager would file someone else's
    guessing in their history.
    """
    for refusal in refusals:
        record_audit(
            action=AuditAction.DISCOUNT_REFUSED,
            entity_type="accounts.User",
            entity_id=refusal["username"],
            actor=cashier,
            request=request,
            reason=refusal["reason_code"],
            after={
                "attempted_authorizer": refusal["username"],
                "acting_cashier": cashier.username,
                "offline": True,
                "device": device.name if device else None,
                "occurred_at": refusal["occurred_at"].isoformat(),
            },
        )
    return len(refusals)
