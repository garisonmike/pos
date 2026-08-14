"""Abstract bases and the audit trail every app writes to."""

from __future__ import annotations

import uuid

from django.db import models

from apps.core.managers import TenantManager


class TimeStampedModel(models.Model):
    """Adds creation and modification timestamps.

    Almost everything in a POS needs to answer "when did this happen", and
    reconstructing it later from an audit log is far more work than storing it
    once here.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """Primary keys that a disconnected till can generate for itself.

    Sequential integer keys would require a round trip to the server before a
    record could be referenced, which is impossible during a network outage.
    UUIDs let the Flutter client create a sale, reference it from its lines and
    payments, and sync the whole graph later without renumbering anything.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TenantOwnedModel(models.Model):
    """Base for every model whose rows belong to exactly one business.

    Inheriting from this is what makes a model eligible for tenant isolation,
    but it is not sufficient on its own: the migration that creates the table
    must also apply the Row-Level Security policy from ``apps.core.db.rls``.
    A test walks every subclass of this class and fails the build if any of
    them is missing its policy, so the two cannot drift apart.

    ``PROTECT`` is used rather than ``CASCADE`` because deleting a tenant must
    never be a single click that silently destroys a business's sales history.
    Tenants are suspended, not deleted.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    objects = TenantManager()
    # ruff reads `models.Manager()` as a field declaration and so flags this as
    # a field following a manager. Both lines here are managers, in the order
    # the style guide asks for.
    all_objects = models.Manager()  # noqa: DJ012

    class Meta:
        abstract = True


class AuditAction(models.TextChoices):
    """What happened, in terms a shop owner would recognise."""

    CREATE = "CREATE", "Created"
    UPDATE = "UPDATE", "Updated"
    DELETE = "DELETE", "Deleted"
    DEACTIVATE = "DEACTIVATE", "Deactivated"
    REACTIVATE = "REACTIVATE", "Reactivated"
    LOGIN = "LOGIN", "Signed in"
    LOGIN_FAILED = "LOGIN_FAILED", "Sign-in refused"
    SUSPEND = "SUSPEND", "Suspended"
    STOCK_ADJUST = "STOCK_ADJUST", "Stock adjusted"
    VOID = "VOID", "Voided"
    REFUND = "REFUND", "Refunded"
    DISCOUNT_AUTHORIZED = "DISCOUNT_AUTHORIZED", "Discount authorised"
    DISCOUNT_REFUSED = "DISCOUNT_REFUSED", "Discount authorisation refused"
    SHIFT_OPENED = "SHIFT_OPENED", "Shift opened"
    SHIFT_CLOSED = "SHIFT_CLOSED", "Shift closed"
    CASH_MOVEMENT = "CASH_MOVEMENT", "Cash moved in or out of the drawer"


class AuditLog(TimeStampedModel):
    """An append-only record of who changed what, when, and why.

    Deliberately not a ``TenantOwnedModel`` even though it carries a tenant,
    because platform-level actions - provisioning a business, suspending one -
    have no tenant of their own and still need recording. The isolation policy
    applied to this table is the same one used everywhere else, so a tenant can
    read its own entries and nothing else, while rows with no tenant are
    visible only from the platform surface.

    ``before`` and ``after`` hold the changed fields rather than whole objects.
    Storing everything would bloat the table and, more importantly, would copy
    password hashes and PIN hashes into a table that managers can read.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="audit_logs",
        null=True,
        blank=True,
        help_text="Null for platform-level actions that predate or outlive a tenant.",
    )
    actor = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
        help_text="Null when the actor was deleted or the action was automated.",
    )
    actor_label = models.CharField(
        max_length=150,
        blank=True,
        help_text=(
            "Who acted, captured as text at the time. Survives the user record "
            "being renamed or removed, which is the whole point of an audit trail."
        ),
    )
    action = models.CharField(max_length=32, choices=AuditAction.choices)
    entity_type = models.CharField(
        max_length=64,
        help_text="Model label, for example 'catalog.Item'.",
    )
    entity_id = models.CharField(max_length=64, blank=True)
    reason = models.TextField(
        blank=True,
        help_text="Free text supplied by the actor. Required for stock adjustments and voids.",
    )
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)

    # A plain manager: this model carries a tenant but does not inherit
    # TenantOwnedModel, because platform-level entries have no tenant at all.
    # Scoping is left entirely to the database policy. ``all_objects`` exists
    # so callers can read the same way they do on tenant-owned models.
    objects = models.Manager()
    all_objects = models.Manager()

    class Meta:
        db_table = "core_audit_log"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.entity_type}:{self.entity_id} by {self.actor_label}"
