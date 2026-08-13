"""
The platform console.

Django's admin is used as the operator's control surface because it is free,
and everything it needs to do - create a business, suspend one, look at usage -
is standard CRUD over a handful of models. The REST endpoints in this app exist
alongside it, so replacing this with a purpose-built dashboard later is
additive rather than a rewrite.

Two things harden it beyond the default. It is mounted at a configurable path
rather than ``/admin/``, and access requires ``is_platform_admin`` rather than
merely ``is_staff``. The second is the one that matters: ``is_staff`` is a flag
a bug could plausibly set on a tenant's user, whereas ``is_platform_admin`` is
checked by a database constraint that forbids it on anyone who belongs to a
business.
"""

from __future__ import annotations

from django.contrib.admin import AdminSite


class PlatformAdminSite(AdminSite):
    """An admin site only the platform operator can reach."""

    site_header = "POS platform console"
    site_title = "POS platform"
    index_title = "Businesses on this platform"

    def has_permission(self, request) -> bool:
        """Require an active platform administrator.

        Deliberately stricter than the default, which accepts any active staff
        user. A tenant's owner is staff of their own shop in every ordinary
        sense of the word, and must never reach this console.
        """
        user = getattr(request, "user", None)
        return bool(
            user
            and user.is_active
            and user.is_authenticated
            and getattr(user, "is_platform_admin", False)
        )


platform_admin_site = PlatformAdminSite(name="platform_admin")
