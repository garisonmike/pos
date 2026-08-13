"""
Serializers for sign-in, users and devices.

The sign-in serializers all follow the same shape: resolve the business from
its slug first, bind it, and only then read the user table. That order is
forced by tenant isolation - before a tenant is bound, the user table returns
nothing at all - and it has a useful side effect: an attacker cannot use these
endpoints to discover whether a username exists somewhere on the platform,
only whether it exists in a business whose slug they already knew.
"""

from __future__ import annotations

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.accounts.constants import UserRole
from apps.accounts.models import Device, User
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant


class UserSerializer(serializers.ModelSerializer):
    """A user as the business sees them. Never exposes credentials."""

    role_label = serializers.CharField(read_only=True)
    has_pin = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "full_name",
            "phone",
            "email",
            "role",
            "role_label",
            "store",
            "is_active",
            "has_pin",
            "last_login",
            "created_at",
        )
        read_only_fields = ("id", "last_login", "created_at")

    def get_has_pin(self, obj: User) -> bool:
        """Whether fast till sign-in is set up, without revealing the PIN itself."""
        return bool(obj.pin_hash)


class UserCreateSerializer(serializers.ModelSerializer):
    """Adding a member of staff to a business.

    The tenant is taken from the request, never from the payload. Accepting it
    from the client would let an authenticated owner of one shop create a user
    inside another, which the database would refuse anyway - but failing at the
    serializer gives a clear error instead of an opaque constraint violation.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    pin = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Optional 4 to 6 digit PIN for fast sign-in at a registered till.",
    )

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "full_name",
            "phone",
            "email",
            "role",
            "store",
            "password",
            "pin",
        )
        read_only_fields = ("id",)

    def validate_role(self, value: str) -> str:
        """Refuse roles that do not exist inside a business."""
        if value not in UserRole.values:
            raise serializers.ValidationError("Unknown role.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def validate_pin(self, value: str) -> str:
        if value and not (value.isdigit() and 4 <= len(value) <= 6):
            raise serializers.ValidationError("A PIN must be 4 to 6 digits.")
        return value

    def validate_username(self, value: str) -> str:
        tenant = self.context["request"].user.tenant
        if User.objects.filter(tenant=tenant, username=value).exists():
            raise serializers.ValidationError(
                "Someone in this business already uses that username."
            )
        return value

    def create(self, validated_data: dict) -> User:
        pin = validated_data.pop("pin", "")
        password = validated_data.pop("password")
        user = User(tenant=self.context["request"].user.tenant, **validated_data)
        user.set_password(password)
        if pin:
            user.set_pin(pin)
        user.save()
        return user


class TenantLoginSerializer(serializers.Serializer):
    """Full sign-in: business slug, username and password.

    Used when a device is first set up, and whenever a manager or owner needs
    to sign in properly rather than switching in at the till with a PIN.
    """

    tenant_slug = serializers.SlugField(
        help_text="The business identifier entered once when the till was set up."
    )
    username = serializers.CharField()
    password = serializers.CharField(
        write_only=True, style={"input_type": "password"}, trim_whitespace=False
    )

    #: One message for every failure, so that a wrong username and a wrong
    #: password are indistinguishable to someone probing the endpoint.
    _REFUSED = "Those sign-in details were not recognised."

    def validate(self, attrs: dict) -> dict:
        tenant = Tenant.objects.filter(slug=attrs["tenant_slug"]).first()
        if tenant is None:
            raise serializers.ValidationError({"detail": self._REFUSED})
        if not tenant.is_operational:
            raise serializers.ValidationError(
                {
                    "detail": (
                        "This business account is not active. Contact your "
                        "provider to restore access."
                    )
                }
            )

        # The user table is tenant-isolated, so nothing can be read from it
        # until the business is bound.
        with tenant_context(tenant.id):
            user = authenticate(
                request=self.context.get("request"),
                username=attrs["username"],
                password=attrs["password"],
            )
            # authenticate() goes through Django's backend, which resolves
            # platform administrators only. Tenant users are checked directly.
            if user is None:
                candidate = User.objects.filter(
                    tenant=tenant, username=attrs["username"]
                ).first()
                if candidate and candidate.check_password(attrs["password"]):
                    user = candidate

            if user is None or not user.is_active or user.tenant_id != tenant.id:
                raise serializers.ValidationError({"detail": self._REFUSED})

        attrs["user"] = user
        attrs["tenant"] = tenant
        return attrs


class PinLoginSerializer(serializers.Serializer):
    """Fast till sign-in: a registered device plus a short PIN.

    The device token is half of this credential and the PIN is the other half.
    A four-digit PIN alone would be trivially guessable; combined with
    possession of a registered till it is a reasonable way to attribute sales
    to the right cashier without a password between every customer.

    This validates the shape of the request only. The credential check itself
    is in ``apps.accounts.services.authenticate_pin``, because the view has to
    distinguish a locked-out till from a wrong PIN from an unregistered device,
    and apply the lockout counters in between - none of which fits inside a
    serializer that can only pass or raise.
    """

    tenant_slug = serializers.SlugField()
    device_token = serializers.CharField(write_only=True)
    username = serializers.CharField()
    pin = serializers.CharField(write_only=True, trim_whitespace=False)


class PlatformLoginSerializer(serializers.Serializer):
    """Sign-in for the platform operator, who belongs to no business."""

    username = serializers.CharField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    _REFUSED = "Those sign-in details were not recognised."

    def validate(self, attrs: dict) -> dict:
        user = User.all_objects.filter(
            username=attrs["username"], tenant__isnull=True, is_active=True
        ).first()
        if user is None or not user.check_password(attrs["password"]):
            raise serializers.ValidationError({"detail": self._REFUSED})
        if not user.is_platform_admin:
            raise serializers.ValidationError({"detail": self._REFUSED})
        attrs["user"] = user
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    """Changing one's own password. Requires the current one."""

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value: str) -> str:
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("That is not your current password.")
        return value

    def validate_new_password(self, value: str) -> str:
        validate_password(value, self.context["request"].user)
        return value


class SetPinSerializer(serializers.Serializer):
    """Setting or clearing a user's till PIN."""

    pin = serializers.CharField(
        allow_blank=True, help_text="Blank clears the PIN and disables fast sign-in."
    )

    def validate_pin(self, value: str) -> str:
        if value and not (value.isdigit() and 4 <= len(value) <= 6):
            raise serializers.ValidationError("A PIN must be 4 to 6 digits.")
        return value


class DeviceSerializer(serializers.ModelSerializer):
    """A registered till, as listed by a manager."""

    class Meta:
        model = Device
        fields = ("id", "name", "is_active", "last_seen_at", "created_at")
        read_only_fields = fields


class DeviceRegisterSerializer(serializers.Serializer):
    """Registering a till. The token it returns is shown exactly once."""

    name = serializers.CharField(max_length=100)


class TokenPairSerializer(serializers.Serializer):
    """The access and refresh pair returned by every sign-in endpoint."""

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)
    user = UserSerializer(read_only=True)
