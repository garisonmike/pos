"""
Users, and the devices they sign in on.

Two things here are unusual enough to be worth stating plainly.

**Usernames are unique per business, not globally.** Two different shops can
both employ a cashier who signs in as ``mary``. Requiring global uniqueness
would mean the second shop to onboard discovers its staff names are taken,
which is a poor experience caused entirely by an implementation detail. The
cost is that sign-in needs a business identifier, which the till stores once at
setup and sends automatically thereafter.

**A four-digit PIN is not a password.** It exists so a cashier can take over
the till between customers without typing a password on a tablet. It is only
ever accepted alongside a registered device token, so possession of the till
is part of the credential. PIN sign-in cannot be used from an arbitrary client.
"""

from __future__ import annotations

import hashlib
import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.accounts.constants import ROLE_ORDER, UserRole, role_rank
from apps.core.models import TenantOwnedModel, TimeStampedModel, UUIDModel
from apps.core.tenancy import get_current_tenant_id


class UserManager(BaseUserManager):
    """Creates users and keeps queries inside the current business."""

    use_in_migrations = True

    def get_queryset(self):
        """Scope to the bound tenant, when there is one.

        Mirrors ``apps.core.managers.TenantManager``; it cannot simply reuse it
        because Django requires the user model's default manager to derive from
        ``BaseUserManager``.
        """
        queryset = super().get_queryset()
        tenant_id = get_current_tenant_id()
        if tenant_id is not None:
            queryset = queryset.filter(tenant_id=tenant_id)
        return queryset

    def get_by_natural_key(self, username: str):
        """Resolve a username for Django's own authentication backend.

        Restricted to platform administrators, who are the only users with no
        tenant. That backend drives the Django admin, which is the platform
        control surface; tenant users sign in through the API, where the
        business is identified explicitly and this ambiguity cannot arise.

        Without this restriction, a username that exists in several businesses
        would make the admin login query return more than one row.
        """
        return self.get(**{self.model.USERNAME_FIELD: username, "tenant__isnull": True})

    def create_user(self, username: str, password: str | None = None, **extra):
        """Create a user belonging to a business."""
        if not username:
            raise ValueError("A username is required.")
        user = self.model(username=username, **extra)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_superuser(self, username: str, password: str | None = None, **extra):
        """Create a platform administrator.

        Platform administrators have no tenant, which is what makes their rows
        invisible to every tenant-scoped query, and carry an explicit flag
        rather than a role so that no role change inside a business can produce
        one.
        """
        extra.setdefault("is_platform_admin", True)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("tenant", None)
        extra.setdefault("role", UserRole.OWNER)

        if not extra["is_platform_admin"]:
            raise ValueError("A platform administrator must have is_platform_admin set.")
        if extra.get("tenant") is not None:
            raise ValueError("A platform administrator must not belong to a tenant.")

        return self.create_user(username, password, **extra)


class User(UUIDModel, AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """One person who signs in, either at a till or at the platform console."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
        help_text="Null only for platform administrators, who belong to no business.",
    )
    username = models.CharField(
        max_length=64,
        help_text="Unique within a business, not across the platform.",
    )
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)

    role = models.CharField(
        max_length=20, choices=UserRole.choices, default=UserRole.CASHIER
    )
    is_platform_admin = models.BooleanField(
        default=False,
        help_text=(
            "Grants cross-tenant access to the platform console. Deliberately "
            "separate from role, so that no privilege change inside a business "
            "can reach across businesses."
        ),
    )

    store = models.ForeignKey(
        "stores.Store",
        on_delete=models.SET_NULL,
        related_name="staff",
        null=True,
        blank=True,
        help_text="Which branch this person normally works at. Null means all branches.",
    )

    pin_hash = models.CharField(
        max_length=128,
        blank=True,
        help_text="Hashed till PIN. Empty means this user cannot use fast sign-in.",
    )

    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Users are deactivated rather than deleted so that their name "
            "stays attached to the sales and audit entries they created."
        ),
    )
    is_staff = models.BooleanField(
        default=False, help_text="May sign in to the platform console."
    )
    last_pin_login_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()
    all_objects = models.Manager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "accounts_user"
        ordering = ("full_name",)
        constraints = [
            # Per-business uniqueness. Django's auth.E003 check wants a global
            # unique constraint on USERNAME_FIELD instead; it is silenced in
            # settings, with the reasoning recorded there.
            models.UniqueConstraint(
                fields=["tenant", "username"], name="unique_username_per_tenant"
            ),
            models.UniqueConstraint(
                fields=["username"],
                condition=models.Q(tenant__isnull=True),
                name="unique_platform_admin_username",
            ),
            # A tenant user must have a role that means something inside a
            # business; a platform administrator must have no tenant at all.
            models.CheckConstraint(
                condition=models.Q(is_platform_admin=False) | models.Q(tenant__isnull=True),
                name="platform_admin_has_no_tenant",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "is_active"])]

    def __str__(self) -> str:
        scope = self.tenant.slug if self.tenant_id else "platform"
        return f"{self.full_name} ({self.username}@{scope})"

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.username

    def get_full_name(self) -> str:
        return self.full_name

    def has_role_at_least(self, required_role: str) -> bool:
        """Whether this user's role meets or exceeds the one required.

        Platform administrators are excluded rather than granted everything:
        their reach is cross-tenant, and letting that double as unlimited
        authority inside a specific business would blur two separate ideas.
        Anything they genuinely need lives on the platform surface.
        """
        return role_rank(self.role) >= role_rank(required_role) >= 0

    @property
    def role_label(self) -> str:
        return UserRole(self.role).label if self.role in UserRole.values else self.role

    def set_pin(self, raw_pin: str) -> None:
        """Set the till PIN, hashed with the same algorithm as passwords."""
        if not raw_pin:
            self.pin_hash = ""
            return
        if not (raw_pin.isdigit() and 4 <= len(raw_pin) <= 6):
            raise ValueError("A PIN must be 4 to 6 digits.")
        self.pin_hash = make_password(raw_pin)

    def check_pin(self, raw_pin: str) -> bool:
        """Verify a till PIN. Always false when no PIN has been set."""
        if not self.pin_hash or not raw_pin:
            return False
        return check_password(raw_pin, self.pin_hash)

    def clear_pin(self) -> None:
        self.pin_hash = ""

    @staticmethod
    def assignable_roles() -> tuple[str, ...]:
        """Roles a business owner may hand out, least privileged first."""
        return ROLE_ORDER


def hash_device_token(raw_token: str) -> str:
    """Hash a device token for storage and lookup.

    A plain SHA-256 rather than a password hash, because the token has to be
    looked up by value on every fast sign-in and a deliberately slow hash
    cannot be used as an index. That is acceptable here in a way it would not
    be for a password: the token is 32 bytes of system-generated randomness, so
    there is no dictionary to attack and nothing for a slow hash to protect
    against.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


class Device(TenantOwnedModel, UUIDModel, TimeStampedModel):
    """A till registered to a business.

    Registering a device is what allows PIN sign-in from it, and what lets
    offline sales be attributed to a specific till when they eventually sync.
    Revoking one is how a business responds to a lost or stolen tablet.
    """

    name = models.CharField(
        max_length=100, help_text="How staff refer to it, for example 'Front counter'."
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    registered_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        related_name="registered_devices",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "accounts_device"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @classmethod
    def issue(cls, *, tenant, name: str, registered_by=None) -> tuple[Device, str]:
        """Create a device and return it with its one-time plaintext token.

        The plaintext is returned here and never stored, so it is shown to the
        person setting up the till exactly once. A replacement requires
        registering the device again, which is the correct outcome: a token
        that can be recovered later is a token that can be recovered by someone
        who should not have it.
        """
        raw_token = secrets.token_urlsafe(32)
        device = cls.objects.create(
            tenant=tenant,
            name=name,
            token_hash=hash_device_token(raw_token),
            registered_by=registered_by,
        )
        return device, raw_token

    def touch(self) -> None:
        """Record that this till was seen, for spotting devices gone quiet."""
        self.last_seen_at = timezone.now()
        self.save(update_fields=["last_seen_at", "updated_at"])
