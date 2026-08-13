"""Serializers for a business's own settings and its first-time setup."""

from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.accounts.constants import UserRole
from apps.accounts.models import User
from apps.tenants.models import BusinessType, ModuleKey, Tenant, TenantModule, VatMode
from apps.tenants.templates_registry import BUSINESS_TEMPLATES


class TenantModuleSerializer(serializers.ModelSerializer):
    """One optional capability and whether it is switched on."""

    label = serializers.SerializerMethodField()

    class Meta:
        model = TenantModule
        fields = ("id", "module_key", "label", "is_enabled", "config")
        read_only_fields = ("id", "module_key", "label")

    def get_label(self, obj: TenantModule) -> str:
        if obj.module_key in ModuleKey.values:
            return ModuleKey(obj.module_key).label
        return obj.module_key


class TenantSerializer(serializers.ModelSerializer):
    """A business's own settings, as its owner sees them.

    Status is read-only here on purpose: whether a business may trade is the
    platform operator's decision, not the customer's, and exposing it as
    writable on a tenant-facing endpoint would let a suspended customer
    reinstate themselves.
    """

    modules = TenantModuleSerializer(many=True, read_only=True)
    is_setup_complete = serializers.BooleanField(read_only=True)

    class Meta:
        model = Tenant
        fields = (
            "id",
            "name",
            "slug",
            "business_type",
            "status",
            "country",
            "currency",
            "timezone",
            "vat_mode",
            "kra_pin",
            "phone",
            "email",
            "address",
            "logo",
            "receipt_header",
            "receipt_footer",
            "trial_ends_at",
            "is_setup_complete",
            "modules",
            "created_at",
        )
        read_only_fields = (
            "id",
            "slug",
            "status",
            "trial_ends_at",
            "is_setup_complete",
            "modules",
            "created_at",
        )


class StaffSetupSerializer(serializers.Serializer):
    """One member of staff added during the setup wizard."""

    username = serializers.CharField(max_length=64)
    full_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)
    pin = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=UserRole.choices, default=UserRole.CASHIER)

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def validate_pin(self, value: str) -> str:
        if value and not (value.isdigit() and 4 <= len(value) <= 6):
            raise serializers.ValidationError("A PIN must be 4 to 6 digits.")
        return value


class TenantSetupSerializer(serializers.Serializer):
    """The owner's first-time setup.

    Everything here has a working default, because the fastest useful setup is
    one where a shop owner taps through and starts selling. The defaults are
    Kenyan retail: one branch, VAT at 16% included in the marked price.
    """

    business_type = serializers.ChoiceField(
        choices=BusinessType.choices, default=BusinessType.RETAIL
    )
    vat_mode = serializers.ChoiceField(choices=VatMode.choices, default=VatMode.INCLUSIVE)

    store_name = serializers.CharField(max_length=120, default="Main")
    store_code = serializers.CharField(max_length=20, default="MAIN")

    tax_rate_name = serializers.CharField(max_length=60, default="VAT 16%")
    tax_rate_bps = serializers.IntegerField(
        default=1600,
        min_value=0,
        max_value=10_000,
        help_text="Basis points. 1600 is 16%; use 0 for a zero-rated business.",
    )
    tax_is_inclusive = serializers.BooleanField(default=True)

    staff = StaffSetupSerializer(many=True, required=False)

    def validate_staff(self, value: list[dict]) -> list[dict]:
        """Reject duplicate usernames before any account is created.

        Catching this here rather than letting the unique constraint fire means
        the owner is told which name clashed, instead of receiving a conflict
        after some of their staff were already created.
        """
        usernames = [member["username"] for member in value]
        duplicates = {name for name in usernames if usernames.count(name) > 1}
        if duplicates:
            raise serializers.ValidationError(
                f"Repeated username(s): {', '.join(sorted(duplicates))}."
            )

        tenant = self.context["request"].user.tenant
        taken = set(
            User.objects.filter(tenant=tenant, username__in=usernames).values_list(
                "username", flat=True
            )
        )
        if taken:
            raise serializers.ValidationError(
                f"Already in use in this business: {', '.join(sorted(taken))}."
            )
        return value


class BusinessTemplateSerializer(serializers.Serializer):
    """A business-type template, offered as a choice during setup."""

    business_type = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField()
    enabled_modules = serializers.ListField(child=serializers.CharField())
    default_vat_mode = serializers.CharField()
    default_tax_rate_bps = serializers.IntegerField()
    tracks_stock_by_default = serializers.BooleanField()

    @classmethod
    def all_templates(cls) -> list[dict]:
        """Every template, for the wizard's business-type picker."""
        return [
            {
                "business_type": template.business_type,
                "label": template.label,
                "description": template.description,
                "enabled_modules": list(template.enabled_modules),
                "default_vat_mode": template.default_vat_mode,
                "default_tax_rate_bps": template.default_tax_rate_bps,
                "tracks_stock_by_default": template.tracks_stock_by_default,
            }
            for template in BUSINESS_TEMPLATES.values()
        ]
