"""Serializers for the platform operator's surface."""

from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.tenants.models import BusinessType, Tenant, TenantStatus


class PlatformTenantSerializer(serializers.ModelSerializer):
    """A business, as the platform operator sees it.

    Includes ``notes``, which the business itself never sees, and ``status``,
    which only the operator may change.
    """

    user_count = serializers.IntegerField(read_only=True)
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
            "trial_ends_at",
            "setup_completed_at",
            "is_setup_complete",
            "notes",
            "user_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "slug", "setup_completed_at", "created_at", "updated_at")


class TenantProvisionSerializer(serializers.Serializer):
    """Onboarding a new customer.

    Creates the business and the single account its owner signs in with. The
    owner then runs the setup wizard themselves, because how they price and
    what they call their branch is not something the operator can know.
    """

    name = serializers.CharField(max_length=150)
    slug = serializers.SlugField(
        max_length=60,
        required=False,
        allow_blank=True,
        help_text="Derived from the name when omitted. Staff type this at sign-in.",
    )
    business_type = serializers.ChoiceField(
        choices=BusinessType.choices, default=BusinessType.RETAIL
    )
    status = serializers.ChoiceField(
        choices=TenantStatus.choices, default=TenantStatus.TRIAL
    )
    trial_days = serializers.IntegerField(default=30, min_value=0, max_value=365)

    owner_username = serializers.CharField(max_length=64)
    owner_full_name = serializers.CharField(max_length=150)
    owner_password = serializers.CharField(write_only=True)
    owner_phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    owner_email = serializers.EmailField(required=False, allow_blank=True)

    def validate_slug(self, value: str) -> str:
        if value and Tenant.objects.filter(slug=value).exists():
            raise serializers.ValidationError("That identifier is already taken.")
        return value

    def validate_owner_password(self, value: str) -> str:
        validate_password(value)
        return value


class TenantStatusChangeSerializer(serializers.Serializer):
    """Suspending or reactivating a business.

    A reason is required for suspension. It goes into the audit trail, which is
    what makes it possible to answer "why was this shop cut off" months later
    without relying on anyone's memory.
    """

    reason = serializers.CharField(max_length=500, allow_blank=True, required=False)


class TenantUsageSerializer(serializers.Serializer):
    """Per-business counts, for the operator's own invoicing."""

    tenant_id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()
    business_type = serializers.CharField()
    status = serializers.CharField()
    is_setup_complete = serializers.BooleanField()
    user_count = serializers.IntegerField()
    active_user_count = serializers.IntegerField()
    store_count = serializers.IntegerField()
    device_count = serializers.IntegerField()
    item_count = serializers.IntegerField()
    enabled_modules = serializers.ListField(child=serializers.CharField())
    last_activity_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
