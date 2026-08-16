"""
Who may take money off a sale.

A discount reduces what the customer pays, so it is the simplest way to move
money out of a shop: ring up a bag of sugar, discount it to nothing, pocket the
difference. That makes it an authority question rather than a data-entry one.

Two paths, chosen by the acting user's own role:

**Manager or owner at the till.** Their session already carries that authority -
it is the same authority every other manager-gated action in this system relies
on - so they authorise under their own identity with no credential re-entry. A
reason is still required.

**Cashier at the till.** Someone with authority has to come and give it, and
give it *now*: a manager's username plus their own PIN or password, verified
against the credential in this request. A manager identified by id alone would
not be a gate at all - if the cashier can type the id, the cashier can type any
id, and the control would be theatre.

There is no self-reference check, deliberately. In the cashier path a cashier
naming themselves already fails on role, and in the manager path referring to
oneself is exactly what a manager running the till alone is doing.

Nothing here signs anyone in. No token is issued, no session changes, and
``request.user`` remains the cashier throughout. A manager's credential is
verified and discarded.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from apps.accounts.constants import UserRole
from apps.accounts.models import User
from apps.core.audit import record_audit
from apps.core.models import AuditAction


class AuthorizationMethod:
    """How authority for a discount was established."""

    SESSION = "SESSION"
    PIN = "PIN"
    PASSWORD = "PASSWORD"
    #: Verified on the device while it had no connection. Weaker than the
    #: others, and recorded as such rather than quietly equated with them.
    OFFLINE = "OFFLINE"


class DiscountNotAuthorized(Exception):
    """A discount that nobody with authority approved."""

    def __init__(self, detail: str, code: str = "discount_not_authorized"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


#: One message for every credential failure, so the endpoint cannot be used to
#: work out which staff names exist or which of them are managers. The specific
#: reason goes to the audit trail, where a manager can read it.
_REFUSED = "That authorisation was not accepted."


@dataclass(frozen=True)
class DiscountAuthorization:
    """Proof that someone with authority approved a discount.

    Carries the authoriser as both a foreign key and a label, following the
    audit trail's pattern: a manager who is later renamed or removed must not
    blank the record of what they approved.
    """

    user: User
    label: str
    reason: str
    via: str
    at: object

    def as_sale_fields(self) -> dict:
        """The columns this writes onto a sale."""
        return {
            "discount_authorized_by": self.user,
            "discount_authorized_label": self.label,
            "discount_authorization_reason": self.reason,
            "discount_authorized_via": self.via,
            "discount_authorized_at": self.at,
        }


def resolve_discount_authorization(
    *,
    actor: User,
    payload: dict | None,
    request=None,
    refused_action: str = AuditAction.DISCOUNT_REFUSED,
) -> DiscountAuthorization:
    """Establish manager authority for a sensitive action, or refuse.

    Named for the first thing that needed it. It is the general mechanism now -
    voiding an order that has already reached the kitchen goes through exactly
    this, because the question is identical: is somebody with authority here,
    and can they prove it right now?

    ``refused_action`` is the audit action a refusal is filed under, so a
    refused void does not appear in the history as a refused discount. It
    defaults to the discount case, which is why no existing caller changed.

    Called before anything is written, so a refusal leaves nothing behind.
    """
    payload = payload or {}
    reason = str(payload.get("reason", "")).strip()

    if actor.has_role_at_least(UserRole.MANAGER):
        return _authorize_from_session(actor=actor, reason=reason, request=request)

    return _authorize_from_credential(
        actor=actor,
        payload=payload,
        reason=reason,
        request=request,
        refused_action=refused_action,
    )


def _authorize_from_session(*, actor: User, reason: str, request) -> DiscountAuthorization:
    """A manager or owner authorising under their own signed-in identity.

    No PIN re-entry. Their session is the same authority that already lets them
    adjust stock, refund a sale and register a till; asking them to prove it
    again at the till would be a ritual, and rituals get worked around.
    """
    if not reason:
        raise DiscountNotAuthorized(
            "A discount needs a reason.", "discount_reason_required"
        )

    return DiscountAuthorization(
        user=actor,
        label=actor.username,
        reason=reason,
        via=AuthorizationMethod.SESSION,
        at=timezone.now(),
    )


def _authorize_from_credential(
    *,
    actor: User,
    payload: dict,
    reason: str,
    request,
    refused_action: str = AuditAction.DISCOUNT_REFUSED,
) -> DiscountAuthorization:
    """A cashier delegating to someone who is present and can prove it."""
    username = str(payload.get("username", "")).strip()
    pin = str(payload.get("pin", ""))
    password = str(payload.get("password", ""))

    if not username or not (pin or password):
        _record_refusal(
            actor=actor,
            username=username,
            reason_code="no_authorization",
            request=request,
            action=refused_action,
        )
        raise DiscountNotAuthorized(
            "A discount needs a manager's approval. Ask a manager to enter "
            "their username and PIN.",
            "discount_authorization_required",
        )

    if not reason:
        raise DiscountNotAuthorized(
            "A discount needs a reason.", "discount_reason_required"
        )

    # Resolved inside the caller's own business. The user table is already
    # tenant-isolated, so a manager from another shop is simply not found -
    # this needs no check of its own.
    manager = User.objects.filter(
        tenant=actor.tenant, username=username, is_active=True
    ).first()

    if manager is None:
        _record_refusal(
            actor=actor,
            username=username,
            reason_code="unknown_user",
            request=request,
            action=refused_action,
        )
        raise DiscountNotAuthorized(_REFUSED)

    verified_via = None
    if pin and manager.check_pin(pin):
        verified_via = AuthorizationMethod.PIN
    elif password and manager.check_password(password):
        verified_via = AuthorizationMethod.PASSWORD

    if verified_via is None:
        # Checked before the role, so a wrong PIN and a wrong role are
        # indistinguishable to the caller: otherwise this endpoint would report
        # which staff hold manager rights.
        _record_refusal(
            actor=actor,
            username=username,
            reason_code="bad_credential",
            request=request,
            action=refused_action,
        )
        raise DiscountNotAuthorized(_REFUSED)

    if not manager.has_role_at_least(UserRole.MANAGER):
        _record_refusal(
            actor=actor,
            username=username,
            reason_code="insufficient_role",
            request=request,
            action=refused_action,
        )
        raise DiscountNotAuthorized(_REFUSED)

    return DiscountAuthorization(
        user=manager,
        label=manager.username,
        reason=reason,
        via=verified_via,
        at=timezone.now(),
    )


def _record_refusal(
    *,
    actor: User,
    username: str,
    reason_code: str,
    request,
    action: str = AuditAction.DISCOUNT_REFUSED,
) -> None:
    """Record a refused authorisation.

    Written here rather than falling out of ``check_pin``, which is a pure
    predicate and audits nothing - the sign-in view writes its own failures the
    same way. Without this, someone standing at a till working through a
    manager's four digits would leave no trace at all, which is precisely the
    trace a shop owner needs.

    Filed against the username *string* with no user foreign key, exactly as a
    failed sign-in is: nobody has proved they are that person, and attaching the
    manager would put someone else's guessing into their history.

    The acting cashier *is* recorded, because unlike a sign-in this happened
    inside an authenticated session - so who was holding the till is known and
    worth knowing.
    """
    record_audit(
        action=action,
        entity_type="accounts.User",
        entity_id=username,
        actor=actor,
        request=request,
        reason=reason_code,
        after={"attempted_authorizer": username, "acting_cashier": actor.username},
    )


def record_authorization(
    *, sale, authorization: DiscountAuthorization, actor, request=None
) -> None:
    """Record a discount that was approved.

    The actor is the cashier and the authoriser is in the payload, so the trail
    reads as the sentence a shop owner needs: this person rang it up, that
    person approved the reduction.
    """
    record_audit(
        action=AuditAction.DISCOUNT_AUTHORIZED,
        entity=sale,
        actor=actor,
        request=request,
        reason=authorization.reason,
        after={
            "authorized_by": authorization.label,
            "via": authorization.via,
            "discount_cents": sale.discount_cents,
            "total_cents": sale.total_cents,
        },
    )
