"""
Role permissions.

Roles are ordered - an Owner can do anything a Manager can, a Manager anything
a Cashier can - so the checks are written as "this role or above" rather than
as membership lists. Adding a role later means inserting it into ``ROLE_ORDER``
rather than revisiting every view.

These sit on top of tenant isolation, not instead of it. A Manager of one shop
carries no permissions at all in another shop's data, because that data is not
visible to their request in the first place.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.constants import UserRole


class IsPlatformAdmin(BasePermission):
    """Allows only the platform operator, never a tenant's own users.

    This is the one permission that grants cross-tenant reach, so it checks a
    dedicated flag rather than a role. No value of ``role`` can produce it, and
    a tenant's Owner is deliberately not a platform administrator: they run
    their shop, not the platform.
    """

    message = "This endpoint is restricted to platform administrators."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, "is_platform_admin", False)
        )


class IsTenantUser(BasePermission):
    """Requires an authenticated user attached to a tenant."""

    message = "This endpoint requires a business account."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user and user.is_authenticated and getattr(user, "tenant_id", None)
        )


class _RoleAtLeast(BasePermission):
    """Shared implementation for the role-threshold permissions below."""

    required_role: str = ""

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated and getattr(user, "tenant_id", None)):
            return False
        return user.has_role_at_least(self.required_role)


class IsCashierOrAbove(_RoleAtLeast):
    """Anyone who works the till: sell, look items up, read the catalogue."""

    message = "You do not have permission to perform this action."
    required_role = UserRole.CASHIER


class IsManagerOrAbove(_RoleAtLeast):
    """Required for anything destructive or anything that changes money.

    Voids, refunds, stock adjustments, price changes and deletions all land
    here. Every one of these is also written to the audit trail, so the pairing
    is deliberate: if a role boundary is worth enforcing, the crossing is worth
    recording.
    """

    message = "This action requires a manager or the business owner."
    required_role = UserRole.MANAGER


class IsOwner(_RoleAtLeast):
    """Business-wide settings: users, tax configuration, receipt branding."""

    message = "This action requires the business owner."
    required_role = UserRole.OWNER


class ReadOnlyOrManager(BasePermission):
    """Everyone in the shop may read; only managers and above may write.

    The common shape for catalogue and configuration endpoints, where a cashier
    needs to look things up all day and must never edit them.
    """

    message = "Changing this requires a manager or the business owner."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated and getattr(user, "tenant_id", None)):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.has_role_at_least(UserRole.MANAGER)
