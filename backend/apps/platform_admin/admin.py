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
from apps.compliance.models import ComplianceDocument
from apps.core.audit import record_audit
from apps.core.middleware import clear_tenant_status_cache
from apps.core.models import AuditAction, AuditLog
from apps.payments.models import MpesaCallback, MpesaCredential
from apps.platform_admin.sites import platform_admin_site
from apps.sales.models import SaleDiscrepancy
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


# ---------------------------------------------------------------------------
# Money that needs a person
#
# Both of these exist because something happened that the software will not
# resolve on its own: a payment arrived for a sale that had moved on, or a sale
# drove stock below zero. Real money sitting unapplied needs a human to notice
# within hours, not at the end of a reporting period - so they are here rather
# than waiting for the reporting milestone.
#
# Read-only throughout. These are ledger-adjacent records, and a console that
# could edit them would be a way to alter the evidence of what happened.
# ---------------------------------------------------------------------------


class OpenIssueFilter(admin.SimpleListFilter):
    """Unresolved first, because that is the only view worth acting on."""

    title = "status"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [("open", "Needs attention"), ("resolved", "Resolved")]

    def queryset(self, request, queryset):
        if self.value() == "open":
            return queryset.filter(resolved_at__isnull=True)
        if self.value() == "resolved":
            return queryset.filter(resolved_at__isnull=False)
        return queryset


@admin.register(MpesaCallback, site=platform_admin_site)
class MpesaCallbackAdmin(admin.ModelAdmin):
    """Every M-Pesa callback received, and what was done with it.

    The suspect ones are the point: a payment that arrived for a voided sale,
    or from an address not on a business's allowlist, is money the shop may be
    holding and has not applied to anything.
    """

    list_display = (
        "created_at",
        "tenant",
        "outcome",
        "suspect_reason",
        "amount_display",
        "mpesa_receipt_number",
        "needs_attention",
    )
    list_filter = ("outcome", "suspect_reason", OpenIssueFilter, "tenant")
    search_fields = ("checkout_request_id", "mpesa_receipt_number", "phone")
    date_hierarchy = "created_at"
    readonly_fields = tuple(
        field.name for field in MpesaCallback._meta.fields
    ) + ("pretty_payload",)
    exclude = ("raw_payload",)

    @admin.display(description="Amount")
    def amount_display(self, obj):
        if obj.amount_cents is None:
            return "-"
        return f"{obj.amount_cents / 100:,.2f}"

    @admin.display(description="Needs attention", boolean=True)
    def needs_attention(self, obj):
        return obj.needs_attention

    @admin.display(description="What Safaricom sent")
    def pretty_payload(self, obj):
        """The raw payload, kept for disputes.

        Shown as stored rather than summarised, because when a customer insists
        they paid, what arrived verbatim is the thing worth reading.
        """
        import json

        return json.dumps(obj.raw_payload, indent=2)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        """A refused callback is evidence. It does not get tidied away."""
        return False


@admin.register(SaleDiscrepancy, site=platform_admin_site)
class SaleDiscrepancyAdmin(admin.ModelAdmin):
    """Sales that need a person to look at them.

    Never a reason to reject a sale - the money already moved - so these are
    recorded and surfaced instead, exactly as a stock adjustment that goes
    negative is surfaced rather than refused.
    """

    list_display = ("created_at", "tenant", "kind", "short_detail", "is_open")
    list_filter = ("kind", OpenIssueFilter, "tenant")
    search_fields = ("detail",)
    date_hierarchy = "created_at"
    readonly_fields = tuple(field.name for field in SaleDiscrepancy._meta.fields)

    @admin.display(description="Detail")
    def short_detail(self, obj):
        return obj.detail[:80] + ("..." if len(obj.detail) > 80 else "")

    @admin.display(description="Open", boolean=True)
    def is_open(self, obj):
        return obj.is_open

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ComplianceDocument, site=platform_admin_site)
class ComplianceDocumentAdmin(admin.ModelAdmin):
    """Tax documents across every business.

    Two things the operator watches here.

    A run of ``FAILED`` submissions means a business is not filing, and the
    business itself may not know - the sales all went through.

    An **unnumbered** document means a shop that is not registered for VAT was
    asked for a tax invoice. One is unremarkable. A steady stream of them means
    a business is probably trading as though it were registered and should be.

    Read-only, like every other window in this console. A compliance document is
    immutable once issued, and an admin that appeared to edit one would be a lie
    about what the system does.
    """

    list_display = (
        "issued_at",
        "tenant",
        "invoice_code_or_unnumbered",
        "kind",
        "gross_cents",
        "adapter",
        "submission_state",
    )
    list_filter = ("submission_state", "kind", "adapter", "tenant")
    search_fields = ("invoice_code", "buyer_pin", "seller_pin")
    date_hierarchy = "issued_at"
    readonly_fields = tuple(field.name for field in ComplianceDocument._meta.fields)

    @admin.display(description="Document", ordering="invoice_number")
    def invoice_code_or_unnumbered(self, obj):
        return obj.invoice_code or "(unnumbered)"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(MpesaCredential, site=platform_admin_site)
class MpesaCredentialAdmin(admin.ModelAdmin):
    """Which businesses have M-Pesa set up, and against which environment.

    Deliberately does not show the credentials themselves. They are encrypted at
    rest, and a console that decrypted them onto a page would undo that for the
    sake of a field nobody needs to read.
    """

    list_display = ("tenant", "shortcode", "environment", "is_active", "last_verified_at")
    list_filter = ("environment", "is_active")
    search_fields = ("shortcode",)
    fields = (
        "tenant",
        "shortcode",
        "environment",
        "is_active",
        "allowed_callback_ips",
        "last_verified_at",
        "last_error",
    )
    readonly_fields = fields

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


# No "view site" link: the console is not a front end for anything.
platform_admin_site.site_url = None
