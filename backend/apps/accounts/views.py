"""
Sign-in, user management and device registration.

Sign-in endpoints are the only unauthenticated routes in the tenant-facing API.
Everything else inherits ``IsAuthenticated`` from the DRF defaults, so a view
that forgets to declare permissions is closed rather than open.
"""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts import backoff, lockout, services
from apps.accounts.models import Device, User, hash_device_token
from apps.accounts.serializers import (
    ChangePasswordSerializer,
    DeviceRegisterSerializer,
    DeviceSerializer,
    PinLoginSerializer,
    SetPinSerializer,
    TenantLoginSerializer,
    TokenPairSerializer,
    UserCreateSerializer,
    UserSerializer,
)
from apps.accounts.tokens import issue_tokens_for
from apps.core.audit import record_audit
from apps.core.models import AuditAction
from apps.core.permissions import IsManagerOrAbove, IsOwner
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant


@extend_schema(tags=["auth"])
class TenantLoginView(APIView):
    """Sign in with a business slug, username and password."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = TenantLoginSerializer
    throttle_scope = "login"

    @extend_schema(
        summary="Sign in",
        description=(
            "Full sign-in for a member of staff. The business slug is entered "
            "once when a till is set up and sent automatically thereafter.\n\n"
            "Returns an access token carrying the business identity, which "
            "every subsequent request is scoped by."
        ),
        request=TenantLoginSerializer,
        responses={
            200: TokenPairSerializer,
            400: OpenApiResponse(description="Details not recognised, or business inactive"),
        },
        examples=[
            OpenApiExample(
                "Cashier signing in",
                value={
                    "tenant_slug": "mama-njeri-duka",
                    "username": "mary",
                    "password": "correct horse battery staple",
                },
                request_only=True,
            )
        ],
    )
    def post(self, request):
        serializer = TenantLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        tenant = serializer.validated_data["tenant"]
        username = serializer.validated_data["username"]

        # Checked before the password is evaluated, so an attempt inside a
        # waiting period is refused without the credential being looked at -
        # otherwise the response time tells an attacker which state they are in.
        state = backoff.check(tenant.id, username)
        if state.is_delayed:
            self._audit_failure(request, tenant, username, reason="backoff")
            return Response(
                {
                    "detail": (
                        "Too many recent sign-in attempts for this account. "
                        f"Try again in {state.retry_after_seconds} second(s)."
                    ),
                    "code": "login_backoff",
                    "retry_after_seconds": state.retry_after_seconds,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            user = services.authenticate_password(
                tenant=tenant, username=username, password=serializer.validated_data["password"]
            )
        except services.PasswordAuthError as exc:
            failed = backoff.record_failure(tenant.id, username)
            self._audit_failure(
                request, tenant, username, reason="bad_password", failures=failed.failures
            )
            # Raised, not returned, so DRF renders this exactly as the
            # serializer renders an unknown business slug. Returning a Response
            # here made `detail` a plain string where the serializer produces a
            # list, and that difference is an oracle: it tells a caller which
            # business slugs are real without them ever guessing a password.
            #
            # The earned delay is deliberately *not* reported here for the same
            # reason - a body that grows a retry hint only for accounts that
            # exist distinguishes them just as well. The wait is announced on
            # the next attempt, by the 429 above, which cannot avoid being
            # distinguishable and is the only response that is.
            #
            # The list around the message is load-bearing, not styling. A
            # serializer normalises its errors into lists, a view raising by
            # hand does not, and the two render differently once the exception
            # handler stringifies them - which is enough to tell the cases
            # apart. Byte-identical is the requirement here.
            raise ValidationError({"detail": [exc.detail]}) from exc

        backoff.clear(tenant.id, username)

        with tenant_context(tenant.id):
            user.last_login = timezone.now()
            user.save(update_fields=["last_login", "updated_at"])
            record_audit(
                action=AuditAction.LOGIN,
                entity=user,
                actor=user,
                request=request,
                tenant_id=tenant.id,
                after={"method": "password"},
            )
            tokens = issue_tokens_for(user)
            body = {**tokens, "user": UserSerializer(user).data}

        return Response(body, status=status.HTTP_200_OK)

    @staticmethod
    def _audit_failure(
        request, tenant, username: str, *, reason: str, failures: int = 0
    ) -> None:
        """Record every refused password attempt, delayed or not.

        Deliberately unconditional. The counter that decides whether to refuse
        the next attempt expires in fifteen minutes, and a slow campaign against
        an owner's account - a few attempts an hour, never enough to earn a
        delay - would leave nothing behind at all if only delayed attempts were
        written down. The pattern is the thing worth keeping, and it is only
        visible in a record that outlives the counter.

        No user is attached, for the reason set out at length in
        ``PinLoginView._audit_failure``: a failed sign-in proves somebody typed
        this name, not that they are that person, and filing a run of failures
        against a real owner puts an attacker's activity in the victim's
        history.
        """
        with tenant_context(tenant.id):
            record_audit(
                action=AuditAction.LOGIN_FAILED,
                entity_type="accounts.User",
                entity_id=username,
                request=request,
                tenant_id=tenant.id,
                reason=reason,
                after={"method": "password", "failures": failures},
            )


@extend_schema(tags=["auth"])
class PinLoginView(APIView):
    """Fast cashier switching on an already-registered till.

    Guarded by a lockout as well as a rate limit. A four-digit PIN is a space
    of ten thousand, so capping how *fast* attempts arrive is not enough on its
    own - a patient attacker with a stolen till just works within the cap. See
    ``apps.accounts.lockout``.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = PinLoginSerializer
    throttle_scope = "pin-login"

    @extend_schema(
        summary="Sign in with a till PIN",
        description=(
            "Fast sign-in for a cashier taking over a till between customers. "
            "Requires a registered device token as well as the PIN, so it "
            "cannot be used from an unregistered client.\n\n"
            "After several consecutive failures the till is locked for a "
            "period and returns 429 with `retry_after_seconds`, regardless of "
            "how slowly the attempts arrived. Every failure is written to the "
            "business's audit trail."
        ),
        request=PinLoginSerializer,
        responses={
            200: TokenPairSerializer,
            400: OpenApiResponse(description="Refused"),
            429: OpenApiResponse(description="Till locked after repeated failures"),
        },
    )
    def post(self, request):
        serializer = PinLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant = Tenant.objects.filter(slug=data["tenant_slug"]).first()
        if tenant is None or not tenant.is_operational:
            # Same message a wrong PIN gets, so the endpoint cannot be used to
            # discover which businesses exist on the platform.
            return Response(
                {"detail": services.REFUSED_MESSAGE, "code": "pin_refused"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Counters are keyed on the hash of the device token rather than the
        # device's primary key, so an attempt carrying a token that matches no
        # device can still be counted.
        device_key = hash_device_token(data["device_token"])
        username = data["username"]

        state = lockout.check(tenant.id, device_key, username)
        if state.is_locked:
            self._audit_failure(
                request, tenant, username, reason="locked_out", attempts=state.attempts
            )
            return Response(
                {
                    "detail": (
                        "This till is locked after too many incorrect PINs. "
                        f"Try again in {state.retry_after_minutes} minute(s), "
                        "or sign in with a password."
                    ),
                    "code": "pin_locked_out",
                    "retry_after_seconds": state.retry_after_seconds,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            result = services.authenticate_pin(
                tenant=tenant,
                device_token=data["device_token"],
                username=username,
                pin=data["pin"],
            )
        except services.PinAuthError as exc:
            body = {"detail": exc.detail, "code": exc.code}
            # DO NOT simplify this to "count every failure". The branch is
            # load-bearing: a wrong PIN counts, an unrecognised device token
            # does not.
            #
            # A device token is 32 bytes of system randomness, so a wrong one is
            # not evidence of anyone guessing a PIN - there is nothing to guess.
            # But counting it would mean anyone who can reach this endpoint could
            # lock out a till they have no other access to, just by posting
            # rubbish tokens with a real business slug and username. That turns a
            # protection against theft into a way to stop a shop trading, which
            # is a denial of service any passer-by could run from a phone.
            #
            # The rate throttle is what bounds unrecognised-token traffic; the
            # lockout is only for failures against a till that genuinely exists.
            if exc.counts_towards_lockout:
                failed = lockout.record_failure(tenant.id, device_key, username)
                self._audit_failure(
                    request, tenant, username, reason=exc.code, attempts=failed.attempts
                )
                if failed.is_locked:
                    body = {
                        "detail": (
                            "This till is now locked after too many incorrect "
                            f"PINs. Try again in {failed.retry_after_minutes} "
                            "minute(s), or sign in with a password."
                        ),
                        "code": "pin_locked_out",
                        "retry_after_seconds": failed.retry_after_seconds,
                    }
                    return Response(body, status=status.HTTP_429_TOO_MANY_REQUESTS)
                body["attempts_remaining"] = lockout.attempts_remaining(failed)
            else:
                self._audit_failure(
                    request, tenant, username, reason=exc.code, attempts=0
                )
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        lockout.clear(tenant.id, device_key, username)

        with tenant_context(tenant.id):
            record_audit(
                action=AuditAction.LOGIN,
                entity=result.user,
                actor=result.user,
                request=request,
                tenant_id=tenant.id,
                after={"method": "pin", "device": str(result.device.pk)},
            )
            tokens = issue_tokens_for(result.user)
            body = {**tokens, "user": UserSerializer(result.user).data}

        return Response(body, status=status.HTTP_200_OK)

    @staticmethod
    def _audit_failure(request, tenant, username: str, *, reason: str, attempts: int) -> None:
        """Record a refused attempt against the business it was aimed at.

        The cache decides whether to refuse the next attempt; this is the
        durable record. A manager looking into a missing float needs to be able
        to see that someone sat trying PINs on the front counter at 11pm, and a
        counter that expires in fifteen minutes will not tell them that.

        No user is attached even when the username exists, because at this point
        the caller has not proved they are that person - filing the entry
        against them would put failures in an innocent cashier's history.
        """
        with tenant_context(tenant.id):
            record_audit(
                action=AuditAction.LOGIN_FAILED,
                # DO NOT "tidy" this into entity=user with an actor FK, even
                # though the username usually matches a real person and the
                # lookup is right there.
                #
                # A failed sign-in proves only that someone typed this name. It
                # does not prove they are that person - in fact the likeliest
                # reading of a run of these is that somebody else was guessing.
                # Attaching the user would file that run in an innocent
                # cashier's history, and this trail is exactly what a manager
                # reads when a float goes missing. The wrong FK here becomes
                # evidence against the victim.
                #
                # So the username is kept as a plain string: enough to see what
                # was tried, not enough to imply who tried it. record_audit also
                # leaves actor null because no authenticated user exists here.
                entity_type="accounts.User",
                entity_id=username,
                request=request,
                tenant_id=tenant.id,
                reason=reason,
                after={"method": "pin", "attempts": attempts},
            )


@extend_schema(tags=["auth"])
class TenantTokenRefreshView(TokenRefreshView):
    """Exchange a refresh token for a new access token.

    This exists rather than using simplejwt's view directly because of where
    the refresh token travels. Every other request carries its token in the
    ``Authorization`` header, which is where ``TenantBindingMiddleware`` looks
    to decide which business the request may see. A refresh request carries it
    in the **body**, and by definition has no usable access token to put in the
    header - that is the whole reason it is being made.

    So the middleware binds nothing, and the user lookup simplejwt performs is
    refused by tenant isolation. The symptom is a till that has been idle long
    enough for its access token to expire and can then never sign back in
    without a full password sign-in, which is precisely the situation refresh
    tokens exist to avoid.

    The fix is to read the tenant from the refresh token's own claim and bind
    it for the exchange.
    """

    @extend_schema(
        summary="Refresh an access token",
        description=(
            "Exchanges a refresh token for a new access token, carrying the "
            "same business identity. Used by the till whenever its access "
            "token expires, including immediately after a spell offline."
        ),
    )
    def post(self, request, *args, **kwargs):
        tenant_id = self._tenant_from_refresh_token(request.data.get("refresh"))
        if tenant_id is None:
            # Either the token is unreadable, in which case the parent view
            # produces a proper 401, or it belongs to a platform administrator
            # who has no business to bind.
            return super().post(request, *args, **kwargs)

        with tenant_context(tenant_id):
            return super().post(request, *args, **kwargs)

    @staticmethod
    def _tenant_from_refresh_token(raw_token: str | None):
        """Read the tenant claim off a refresh token without trusting it further.

        Signature and expiry are verified here; anything beyond that is left to
        the parent view. A token that fails to parse yields ``None`` so the
        caller still gets a proper authentication error rather than a 500.
        """
        if not raw_token:
            return None
        try:
            claim = RefreshToken(raw_token).payload.get("tenant_id")
        except TokenError:
            return None
        return claim or None


@extend_schema(tags=["auth"])
class LogoutView(APIView):
    """Blacklist a refresh token so it cannot be exchanged again."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Sign out",
        description=(
            "Blacklists the supplied refresh token. The access token remains "
            "valid until it expires, which is why access token lifetimes are "
            "kept short."
        ),
        request={
            "application/json": {
                "type": "object",
                "properties": {"refresh": {"type": "string"}},
            }
        },
        responses={
            205: OpenApiResponse(description="Signed out"),
            400: OpenApiResponse(description="Token not usable"),
        },
    )
    def post(self, request):
        raw_refresh = request.data.get("refresh")
        if not raw_refresh:
            return Response(
                {"detail": "A refresh token is required to sign out.", "code": "bad_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            RefreshToken(raw_refresh).blacklist()
        except TokenError:
            return Response(
                {"detail": "That token is no longer usable.", "code": "token_invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_205_RESET_CONTENT)


@extend_schema(tags=["auth"])
class MeView(APIView):
    """The signed-in user, as the client needs them for its own UI."""

    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    @extend_schema(
        summary="Current user",
        description=(
            "Who the caller is, which business they belong to, and what their "
            "role permits. The till uses this to decide which buttons to show."
        ),
        responses={200: UserSerializer},
    )
    def get(self, request):
        data = UserSerializer(request.user).data
        tenant = request.user.tenant
        data["tenant"] = (
            {
                "id": str(tenant.id),
                "name": tenant.name,
                "slug": tenant.slug,
                "business_type": tenant.business_type,
                "currency": tenant.currency,
                "vat_mode": tenant.vat_mode,
                "is_setup_complete": tenant.is_setup_complete,
            }
            if tenant
            else None
        )
        data["is_platform_admin"] = request.user.is_platform_admin
        return Response(data)


@extend_schema(tags=["auth"])
class ChangePasswordView(APIView):
    """Change one's own password."""

    permission_classes = [IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    @extend_schema(
        summary="Change own password",
        request=ChangePasswordSerializer,
        responses={204: OpenApiResponse(description="Changed")},
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])

        record_audit(
            action=AuditAction.UPDATE,
            entity=user,
            actor=user,
            request=request,
            after={"password": "changed"},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["users"])
class UserViewSet(viewsets.ModelViewSet):
    """Staff belonging to the signed-in user's business.

    Reading is open to managers, because a manager running a shift needs to see
    who is on. Creating, editing and deactivating are restricted to the owner,
    since those change who can take money.

    There is no delete. Removing a user would orphan the sales and audit
    entries that carry their name, which defeats the point of keeping an audit
    trail at all, so ``deactivate`` is the only way to remove access.
    """

    serializer_class = UserSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        """Staff of the caller's business, newest sign-ins last.

        The tenant filter is applied by the manager and again by the database
        policy; it is written explicitly here as well so the intent is obvious
        to anyone reading the view on its own.
        """
        # drf-spectacular introspects this without a real request, so
        # request.user is anonymous and has no tenant. Returning an empty
        # queryset lets it derive the model - and therefore correct path
        # parameter types - without weakening anything at runtime.
        if getattr(self, "swagger_fake_view", False):
            return User.objects.none()
        return (
            User.objects.filter(tenant=self.request.user.tenant)
            .select_related("store")
            .order_by("full_name")
        )

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsManagerOrAbove()]
        return [IsOwner()]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action == "set_pin":
            return SetPinSerializer
        return UserSerializer

    @extend_schema(summary="List staff", responses={200: UserSerializer(many=True)})
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Add a member of staff",
        description=(
            "Creates a user inside the caller's business. The business is taken "
            "from the caller's own account and cannot be supplied in the body."
        ),
        request=UserCreateSerializer,
        responses={201: UserSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        record_audit(
            action=AuditAction.CREATE,
            entity=user,
            actor=request.user,
            request=request,
            after={"username": user.username, "role": user.role, "full_name": user.full_name},
        )
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Update a member of staff",
        request=UserSerializer,
        responses={200: UserSerializer},
    )
    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        before = {
            "role": instance.role,
            "full_name": instance.full_name,
            "is_active": instance.is_active,
        }
        response = super().partial_update(request, *args, **kwargs)
        instance.refresh_from_db()

        record_audit(
            action=AuditAction.UPDATE,
            entity=instance,
            actor=request.user,
            request=request,
            before=before,
            after={
                "role": instance.role,
                "full_name": instance.full_name,
                "is_active": instance.is_active,
            },
        )
        return response

    @extend_schema(
        summary="Deactivate a member of staff",
        description=(
            "Removes a person's access without deleting them, so the sales and "
            "audit entries carrying their name stay intact. An owner cannot "
            "deactivate their own account, which would leave the business with "
            "no way back in."
        ),
        request=None,
        responses={200: UserSerializer, 400: OpenApiResponse(description="Refused")},
    )
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        if user.pk == request.user.pk:
            return Response(
                {
                    "detail": "You cannot deactivate your own account.",
                    "code": "self_deactivation",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = False
        user.clear_pin()
        user.save(update_fields=["is_active", "pin_hash", "updated_at"])

        record_audit(
            action=AuditAction.DEACTIVATE,
            entity=user,
            actor=request.user,
            request=request,
            reason=request.data.get("reason", ""),
            before={"is_active": True},
            after={"is_active": False},
        )
        return Response(UserSerializer(user).data)

    @extend_schema(
        summary="Reactivate a member of staff",
        request=None,
        responses={200: UserSerializer},
    )
    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        user = self.get_object()
        user.is_active = True
        user.save(update_fields=["is_active", "updated_at"])

        record_audit(
            action=AuditAction.REACTIVATE,
            entity=user,
            actor=request.user,
            request=request,
            before={"is_active": False},
            after={"is_active": True},
        )
        return Response(UserSerializer(user).data)

    @extend_schema(
        summary="Set or clear a till PIN",
        description="An empty PIN clears it and disables fast sign-in for that user.",
        request=SetPinSerializer,
        responses={204: OpenApiResponse(description="Set")},
    )
    @action(detail=True, methods=["post"], url_path="set-pin")
    def set_pin(self, request, pk=None):
        user = self.get_object()
        serializer = SetPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user.set_pin(serializer.validated_data["pin"])
        user.save(update_fields=["pin_hash", "updated_at"])

        record_audit(
            action=AuditAction.UPDATE,
            entity=user,
            actor=request.user,
            request=request,
            after={"pin": "set" if user.pin_hash else "cleared"},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["devices"])
class DeviceViewSet(viewsets.ReadOnlyModelViewSet):
    """Tills registered to this business.

    Registration returns a token exactly once. It is not recoverable
    afterwards, because a token that can be looked up later can be looked up by
    the wrong person; losing one means registering the till again.
    """

    serializer_class = DeviceSerializer
    permission_classes = [IsManagerOrAbove]
    # A read-only base plus an explicit create: registration is a POST to the
    # list route, but there is no update or delete path, because a token cannot
    # be edited and revoking is an action rather than a deletion.
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        """Tills belonging to the caller's business."""
        # drf-spectacular introspects this without a real request, so
        # request.user is anonymous and has no tenant. Returning an empty
        # queryset lets it derive the model - and therefore correct path
        # parameter types - without weakening anything at runtime.
        if getattr(self, "swagger_fake_view", False):
            return Device.objects.none()
        return Device.objects.filter(tenant=self.request.user.tenant).order_by("name")

    @extend_schema(
        summary="Register a till",
        description=(
            "Registers a device and returns its token. The token is shown once "
            "and never again; it is stored hashed. Enter it on the till during "
            "setup to enable PIN sign-in and offline sales attribution."
        ),
        request=DeviceRegisterSerializer,
        responses={201: OpenApiResponse(description="Registered, token included once")},
    )
    def create(self, request, *args, **kwargs):
        serializer = DeviceRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device, raw_token = Device.issue(
            tenant=request.user.tenant,
            name=serializer.validated_data["name"],
            registered_by=request.user,
        )
        record_audit(
            action=AuditAction.CREATE,
            entity=device,
            actor=request.user,
            request=request,
            after={"name": device.name},
        )
        body = DeviceSerializer(device).data
        body["device_token"] = raw_token
        body["warning"] = "This token is shown only once. Store it on the till now."
        return Response(body, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Revoke a till",
        description="Stops PIN sign-in from this device. Used when a tablet is lost or replaced.",
        request=None,
        responses={200: DeviceSerializer},
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        device = self.get_object()
        device.is_active = False
        device.save(update_fields=["is_active", "updated_at"])

        record_audit(
            action=AuditAction.DEACTIVATE,
            entity=device,
            actor=request.user,
            request=request,
            reason=request.data.get("reason", ""),
            before={"is_active": True},
            after={"is_active": False},
        )
        return Response(DeviceSerializer(device).data)
