"""
Role definitions, kept free of model imports.

This lives in its own module so that permission classes in ``apps.core`` can
import the role ordering without importing the user model, which would create
a cycle: core defines the bases that accounts builds on.
"""

from __future__ import annotations

from django.db import models


class UserRole(models.TextChoices):
    """Who a person is inside one business.

    Named for the shop rather than for the software: an "Owner" is the person
    whose business it is, not a system administrator. The platform operator is
    a separate concept entirely and is marked by a flag, not by a role, so that
    no amount of role escalation inside a tenant can reach across tenants.
    """

    OWNER = "OWNER", "Owner"
    MANAGER = "MANAGER", "Manager"
    CASHIER = "CASHIER", "Cashier"


#: Roles from least to most privileged. Permission checks are expressed as
#: "at least this role", so extending the model later means inserting a name
#: into this list rather than auditing every view.
ROLE_ORDER: tuple[str, ...] = (
    UserRole.CASHIER,
    UserRole.MANAGER,
    UserRole.OWNER,
)


def role_rank(role: str) -> int:
    """Position of a role in the privilege ordering; -1 if unrecognised.

    Unrecognised roles rank below everything rather than raising, so a stale
    token carrying a role that has since been removed loses access instead of
    crashing the request.
    """
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return -1
