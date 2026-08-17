"""
Sign-in logic that is more than field validation.

PIN sign-in lives here rather than in a serializer because the view needs to
tell three outcomes apart and respond differently to each: the till is locked
out (429), the device is not registered (400), or the PIN was wrong (400, and
count it). A serializer's ``validate`` can only raise one kind of error, so
putting this there would collapse distinctions the caller needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from apps.accounts.models import Device, User, hash_device_token
from apps.core.tenancy import tenant_context

#: One message for a wrong username and a wrong PIN alike, so the endpoint
#: cannot be used to work out which cashiers exist.
REFUSED_MESSAGE = "That PIN was not recognised on this device."
UNREGISTERED_MESSAGE = "This till is not registered, or its access was revoked."


class PinAuthError(Exception):
    """A PIN sign-in that did not succeed.

    ``counts_towards_lockout`` distinguishes a wrong credential from a device
    that was never registered. Both are refused, but only the first is evidence
    of someone guessing, and only the first should push a legitimate till
    towards being locked out.
    """

    def __init__(self, detail: str, code: str, *, counts_towards_lockout: bool = True):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.counts_towards_lockout = counts_towards_lockout


@dataclass(frozen=True)
class PinAuthResult:
    """A successful PIN sign-in."""

    user: User
    device: Device


#: One message for a wrong username and a wrong password alike, so the endpoint
#: cannot be used to work out who works at a business.
PASSWORD_REFUSED_MESSAGE = "Those sign-in details were not recognised."


class PasswordAuthError(Exception):
    """A password sign-in that did not succeed."""

    def __init__(self, detail: str = PASSWORD_REFUSED_MESSAGE, code: str = "login_refused"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def authenticate_password(*, tenant, username: str, password: str) -> User:
    """Verify a password against a user of this business.

    Split out of ``TenantLoginSerializer`` for the reason the PIN path is split
    out of ``PinLoginSerializer``: the view has to tell a delayed attempt from
    a wrong password, and count the failure in between, and a serializer that
    can only pass or raise leaves nowhere to do that.

    The tenant is bound before anything is read, because the user table is
    isolated and returns nothing otherwise.
    """
    from django.contrib.auth import authenticate as django_authenticate

    with tenant_context(tenant.id):
        user = django_authenticate(username=username, password=password)

        # django_authenticate goes through the configured backend, which
        # resolves platform administrators only. Tenant users are checked
        # directly against the isolated table.
        if user is None:
            candidate = User.objects.filter(tenant=tenant, username=username).first()
            if candidate and candidate.check_password(password):
                user = candidate

        if user is None or not user.is_active or user.tenant_id != tenant.id:
            raise PasswordAuthError()

    return user


def authenticate_pin(*, tenant, device_token: str, username: str, pin: str) -> PinAuthResult:
    """Verify a till PIN against a registered device.

    The tenant is bound before anything is read, because the device and user
    tables are both isolated and return nothing otherwise. Binding it here
    rather than expecting the caller to means this cannot be called in a way
    that silently sees no data.
    """
    with tenant_context(tenant.id):
        device = Device.objects.filter(
            token_hash=hash_device_token(device_token), is_active=True
        ).first()
        if device is None:
            raise PinAuthError(
                UNREGISTERED_MESSAGE,
                code="device_not_registered",
                counts_towards_lockout=False,
            )

        user = User.objects.filter(
            tenant=tenant, username=username, is_active=True
        ).first()
        if user is None or not user.check_pin(pin):
            raise PinAuthError(REFUSED_MESSAGE, code="pin_refused")

        device.touch()
        user.last_pin_login_at = timezone.now()
        user.save(update_fields=["last_pin_login_at", "updated_at"])

    return PinAuthResult(user=user, device=device)
