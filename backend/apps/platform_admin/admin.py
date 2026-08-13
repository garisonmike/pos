"""
What the platform operator can see and do in the console.

Registrations are on ``platform_admin_site`` rather than the default admin
site, so nothing appears here by accident: a model is visible to the operator
only because someone deliberately added it below.

Note that these views run with tenant isolation lifted, which is what makes
cross-tenant listing possible at all. That is the intended behaviour for this
surface and nowhere else.
"""

from __future__ import annotations

from django.contrib import admin, messages

from apps.accounts.models import Device, User
from apps.core.audit import record_audit
from apps.core.middleware import clear_tenant_status_cache
from apps.core.models import AuditAction, AuditLog
from apps.platform_admin.sites import platform_admin_site
from apps.stores.models import Store
from apps.tenants.models import Tenant, TenantModule


class TenantModuleInline(admin.TabularInline):
    """Switch a business's optional capabilities on or off in place."""

    model = TenantModule
    extra = 0
    fields = ("module_key", "is_enabled", "config")


@admin.register(Tenant, site=platform_admin_site)
class TenantAdmin(admin.ModelAdmin):
    """Businesses on the platform."""

    list_display = ("name", "slug", "business_type", "status", "setup_state", "created_at")
    list_filter = ("status", "business_type", "country")
    search_fields = ("name", "slug", "email", "phone", "kra_pin")
    readonly_fields = ("id", "setup_completed_at", "created_at", "updated_at")
    inlines = [TenantModuleInline]
    actions = ["suspend_selected", "reactivate_selected"]

    fieldsets = (
        (None, {"fields": ("id", "name", "slug", "business_type", "status")}),
        ("Locale", {"fields": ("country", "currency", "timezone")}),
        ("Tax", {"fields": ("vat_mode", "kra_pin")}),
        ("Contact", {"fields": ("phone", "email", "address")}),
        ("Receipt branding", {"fields": ("logo", "receipt_header", "receipt_footer")}),
        (
            "Lifecycle",
            {"fields": ("trial_ends_at", "setup_completed_at", "created_at", "updated_at")},
        ),
        ("Operator notes", {"fields": ("notes",), "description": "Never shown to the business."}),
    )

    @admin.display(description="Setup", boolean=True)
    def setup_state(self, obj: Tenant) -> bool:
        return obj.is_setup_complete

    def has_delete_permission(self, request, obj=None) -> bool:
        """Businesses are never deleted from the console.

        Deleting one would take its sales history with it, which is a legal
        record its owner may need years later. Suspension is the intended way
        to cut off a customer who has stopped paying.
        """
        return False

    @admin.action(description="Suspend selected businesses")
    def suspend_selected(self, request, queryset):
        """Stop the selected businesses from trading, and record who did it."""
        self._bulk_status_change(request, queryset, "SUSPENDED", AuditAction.SUSPEND)

    @admin.action(description="Reactivate selected businesses")
    def reactivate_selected(self, request, queryset):
        self._bulk_status_change(request, queryset, "ACTIVE", AuditAction.REACTIVATE)

    def _bulk_status_change(self, request, queryset, new_status: str, action: str) -> None:
        changed = 0
        for tenant in queryset:
            previous = tenant.status
            if previous == new_status:
                continue
            tenant.status = new_status
            tenant.save(update_fields=["status", "updated_at"])
            clear_tenant_status_cache(tenant.id)
            record_audit(
                action=action,
                entity=tenant,
                actor=request.user,
                request=request,
                tenant_id=None,
                reason="Changed from the platform console.",
                before={"status": previous},
                after={"status": new_status},
            )
            changed += 1
        self.message_user(
            request, f"{changed} business(es) updated.", level=messages.SUCCESS
        )


@admin.register(User, site=platform_admin_site)
class UserAdmin(admin.ModelAdmin):
    """Every account on the platform, across all businesses.

    Read-mostly by design. Support work sometimes needs to confirm an account
    exists or reactivate one, but changing a password from here would create a
    route into a customer's data that leaves nothing in their own audit trail.
    """

    list_display = ("username", "full_name", "tenant", "role", "is_platform_admin", "is_active")
    list_filter = ("role", "is_platform_admin", "is_active", "tenant")
    search_fields = ("username", "full_name", "email", "phone")
    readonly_fields = ("id", "password", "pin_hash", "last_login", "created_at", "updated_at")
    fields = (
        "id",
        "tenant",
        "username",
        "full_name",
        "phone",
        "email",
        "role",
        "store",
        "is_active",
        "is_staff",
        "is_platform_admin",
        "password",
        "pin_hash",
        "last_login",
        "created_at",
        "updated_at",
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        """Users are deactivated, never deleted, so their audit trail survives."""
        return False


@admin.register(Store, site=platform_admin_site)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "tenant", "is_default", "is_active")
    list_filter = ("is_active", "is_default", "tenant")
    search_fields = ("name", "code")


@admin.register(Device, site=platform_admin_site)
class DeviceAdmin(admin.ModelAdmin):
    """Registered tills. Tokens are stored hashed and are not visible here."""

    list_display = ("name", "tenant", "is_active", "last_seen_at", "created_at")
    list_filter = ("is_active", "tenant")
    search_fields = ("name",)
    readonly_fields = ("id", "token_hash", "last_seen_at", "created_at", "updated_at")


@admin.register(AuditLog, site=platform_admin_site)
class AuditLogAdmin(admin.ModelAdmin):
    """The audit trail, across every business.

    Entirely read-only. An audit trail that can be edited from the console it
    is meant to hold accountable is not an audit trail.
    """

    list_display = ("created_at", "tenant", "actor_label", "action", "entity_type", "entity_id")
    list_filter = ("action", "entity_type", "tenant")
    search_fields = ("actor_label", "entity_id", "reason")
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


# No "view site" link: the console is not a front end for anything.
platform_admin_site.site_url = None
