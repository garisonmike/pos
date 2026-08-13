"""
Provisioning a business, and getting it ready to trade.

Two operations live here, and they are deliberately separate because they are
performed by different people at different times.

``provision_tenant`` is run by the platform operator when a new customer signs
up. It creates the business, switches on the modules its type implies, and
creates the one account the owner will use to sign in. Nothing about how the
shop actually runs is decided here, because the operator does not know it.

``complete_setup`` is run by the owner, from the till, the first time they sign
in. It records how they price, names their branch, creates their tax rate and
adds their staff. It can only be run once, so that a second attempt cannot
quietly create a duplicate branch or reset a tax rate that sales already
reference.

Both are plain functions rather than serializer logic so that the platform
console, the API and the test suite all go through the same path.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.constants import UserRole
from apps.accounts.models import User
from apps.catalog.models import TaxRate
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.tenancy import tenant_context
from apps.stores.models import Store
from apps.tenants.models import Tenant, TenantModule, TenantStatus
from apps.tenants.templates_registry import get_template, module_defaults


class SetupAlreadyCompleted(Exception):
    """Raised when the setup wizard is run a second time."""


@transaction.atomic
def provision_tenant(
    *,
    name: str,
    business_type: str,
    owner_username: str,
    owner_password: str,
    owner_full_name: str,
    slug: str = "",
    owner_phone: str = "",
    owner_email: str = "",
    status: str = TenantStatus.TRIAL,
    trial_days: int | None = 30,
    actor=None,
) -> tuple[Tenant, User]:
    """Create a business and its owner account.

    Returns the tenant and the owner. Runs in one transaction so a half-created
    business - one with no way to sign in, say - cannot be left behind by a
    failure partway through.

    The caller is expected to already be running with isolation lifted, which
    the platform surfaces do. Creating a tenant from inside a tenant-scoped
    request is not a supported operation and the database would refuse it.
    """
    tenant = Tenant(
        name=name,
        slug=slug or _unique_slug(name),
        business_type=business_type,
        status=status,
        vat_mode=get_template(business_type).default_vat_mode,
    )
    if trial_days and status == TenantStatus.TRIAL:
        tenant.trial_ends_at = timezone.now() + timezone.timedelta(days=trial_days)
    tenant.save()

    apply_module_template(tenant)

    owner = User(
        tenant=tenant,
        username=owner_username,
        full_name=owner_full_name,
        phone=owner_phone,
        email=owner_email,
        role=UserRole.OWNER,
    )
    owner.set_password(owner_password)
    owner.save()

    record_audit(
        action=AuditAction.CREATE,
        entity=tenant,
        actor=actor,
        tenant_id=None,
        after={
            "name": tenant.name,
            "slug": tenant.slug,
            "business_type": tenant.business_type,
            "status": tenant.status,
        },
    )
    return tenant, owner


def apply_module_template(tenant: Tenant) -> None:
    """Create or top up the module rows for a business, per its type.

    Every module gets a row, including the disabled ones, so that "not enabled"
    and "never considered" are the same state. Without that, adding a module to
    the platform later would leave existing businesses with a gap that reads
    differently from an explicit no.

    Applying a template only ever switches modules **on**. It is called again
    when the owner picks a different business type during setup, and taking
    something away there would be wrong: the operator's guess at sign-up and
    the owner's own later choices are both reasons a module might be enabled,
    and a template cannot tell them apart.
    """
    defaults = module_defaults(tenant.business_type)
    existing = {
        module.module_key: module
        for module in TenantModule.all_objects.filter(tenant=tenant)
    }

    missing = [
        TenantModule(tenant=tenant, module_key=key, is_enabled=enabled)
        for key, enabled in defaults.items()
        if key not in existing
    ]
    if missing:
        TenantModule.all_objects.bulk_create(missing)

    newly_enabled = [
        existing[key]
        for key, enabled in defaults.items()
        if enabled and key in existing and not existing[key].is_enabled
    ]
    for module in newly_enabled:
        module.is_enabled = True
    if newly_enabled:
        TenantModule.all_objects.bulk_update(newly_enabled, ["is_enabled"])


@transaction.atomic
def complete_setup(
    *,
    tenant: Tenant,
    business_type: str,
    vat_mode: str,
    store_name: str,
    store_code: str,
    tax_rate_name: str,
    tax_rate_bps: int,
    tax_is_inclusive: bool,
    staff: list[dict] | None = None,
    actor=None,
) -> Tenant:
    """Run the owner's first-time setup. Only ever once.

    Creates the default branch, the default tax rate and any staff accounts the
    owner adds, then stamps the tenant as set up.
    """
    if tenant.is_setup_complete:
        raise SetupAlreadyCompleted(
            "This business has already been set up. Change settings individually instead."
        )

    with tenant_context(tenant.id):
        tenant.business_type = business_type
        tenant.vat_mode = vat_mode
        tenant.setup_completed_at = timezone.now()
        tenant.save(
            update_fields=["business_type", "vat_mode", "setup_completed_at", "updated_at"]
        )

        # Modules follow the business type chosen here, which may differ from
        # the one the platform operator guessed at sign-up.
        apply_module_template(tenant)

        store = Store.objects.create(
            tenant=tenant,
            name=store_name,
            code=store_code.strip().upper(),
            is_default=True,
        )

        tax_rate = TaxRate.objects.create(
            tenant=tenant,
            name=tax_rate_name,
            rate_bps=tax_rate_bps,
            is_inclusive=tax_is_inclusive,
            is_default=True,
        )

        created_staff = []
        for member in staff or []:
            user = User(
                tenant=tenant,
                username=member["username"],
                full_name=member["full_name"],
                phone=member.get("phone", ""),
                role=member.get("role", UserRole.CASHIER),
                store=store,
            )
            user.set_password(member["password"])
            if member.get("pin"):
                user.set_pin(member["pin"])
            user.save()
            created_staff.append(user.username)

        record_audit(
            action=AuditAction.UPDATE,
            entity=tenant,
            actor=actor,
            tenant_id=tenant.id,
            after={
                "setup": "completed",
                "business_type": business_type,
                "vat_mode": vat_mode,
                "store": store.code,
                "tax_rate": tax_rate.name,
                "staff_added": len(created_staff),
            },
        )

    return tenant


def _unique_slug(name: str) -> str:
    """Derive a slug from a business name, adding a suffix only on collision."""
    base = slugify(name)[:50] or "business"
    slug = base
    suffix = 2
    while Tenant.objects.filter(slug=slug).exists():
        slug = f"{base}-{suffix}"[:60]
        suffix += 1
    return slug
