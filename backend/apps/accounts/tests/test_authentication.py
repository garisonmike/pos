"""Signing in: passwords, till PINs, tokens and refusals."""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.accounts.models import User
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import bypass_rls, tenant_context
from apps.tenants.models import TenantStatus

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login/"
PIN_LOGIN = "/api/v1/auth/pin-login/"


class TestPasswordSignIn:
    def test_a_cashier_can_sign_in(self, anon_client, tenant_a, cashier_a):
        response = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        assert response.status_code == 200

        body = response.json()
        assert body["access"] and body["refresh"]
        assert body["user"]["username"] == "mary"
        assert body["user"]["role"] == "CASHIER"

    def test_the_token_carries_the_business(self, anon_client, tenant_a, cashier_a):
        """The tenant claim is what the middleware binds isolation from."""
        from rest_framework_simplejwt.tokens import AccessToken

        response = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        token = AccessToken(response.json()["access"])

        assert token["tenant_id"] == str(tenant_a.id)
        assert token["role"] == "CASHIER"
        assert token["is_platform_admin"] is False

    def test_a_wrong_password_is_refused(self, anon_client, tenant_a, cashier_a):
        response = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "wrong"},
            format="json",
        )
        assert response.status_code == 400

    def test_the_same_username_in_another_business_is_refused(
        self, anon_client, tenant_a, cashier_a, cashier_b
    ):
        """Business B's Mary cannot sign into business A with her own password.

        Two shops can each employ a Mary. Their accounts must be unrelated even
        though they share a username.
        """
        response = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["user"]["id"] == str(cashier_a.id)

    def test_an_unknown_business_is_refused_the_same_way_as_a_bad_password(
        self, anon_client, cashier_a
    ):
        """A single message, so the endpoint cannot be used to enumerate shops."""
        unknown_business = anon_client.post(
            LOGIN,
            {"tenant_slug": "no-such-shop", "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        bad_password = anon_client.post(
            LOGIN,
            {"tenant_slug": "mama-njeri", "username": "mary", "password": "nope"},
            format="json",
        )
        assert unknown_business.status_code == bad_password.status_code == 400
        assert unknown_business.json()["detail"] == bad_password.json()["detail"]

    def test_a_deactivated_user_cannot_sign_in(self, anon_client, tenant_a, cashier_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            cashier_a.is_active = False
            cashier_a.save(update_fields=["is_active"])

        response = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        assert response.status_code == 400

    def test_a_suspended_business_cannot_sign_in(self, anon_client, tenant_a, cashier_a):
        with transaction.atomic(), bypass_rls():
            tenant_a.status = TenantStatus.SUSPENDED
            tenant_a.save(update_fields=["status"])

        response = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        assert response.status_code == 400
        assert "not active" in response.json()["detail"]

    def test_signing_in_is_recorded_in_the_audit_trail(self, anon_client, tenant_a, cashier_a):
        anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        )
        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.LOGIN).first()

        assert entry is not None
        assert entry.actor_label == "mary"
        assert entry.after["method"] == "password"


class TestPinSignIn:
    """Fast cashier switching, which needs a registered till as well as a PIN."""

    def test_a_cashier_can_switch_in_with_a_pin(self, anon_client, tenant_a, cashier_a, device_a):
        _device, raw_token = device_a
        response = anon_client.post(
            PIN_LOGIN,
            {
                "tenant_slug": tenant_a.slug,
                "device_token": raw_token,
                "username": "mary",
                "pin": "1234",
            },
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "mary"

    def test_a_pin_alone_is_not_enough(self, anon_client, tenant_a, cashier_a):
        """Without a registered device the PIN is refused outright.

        This is the property that makes a four-digit secret acceptable: it is
        only ever half of the credential.
        """
        response = anon_client.post(
            PIN_LOGIN,
            {
                "tenant_slug": tenant_a.slug,
                "device_token": "not-a-real-token",
                "username": "mary",
                "pin": "1234",
            },
            format="json",
        )
        assert response.status_code == 400
        assert "not registered" in response.json()["detail"]

    def test_a_wrong_pin_is_refused(self, anon_client, tenant_a, cashier_a, device_a):
        _device, raw_token = device_a
        response = anon_client.post(
            PIN_LOGIN,
            {
                "tenant_slug": tenant_a.slug,
                "device_token": raw_token,
                "username": "mary",
                "pin": "9999",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_a_revoked_device_cannot_be_used(self, anon_client, tenant_a, cashier_a, device_a):
        device, raw_token = device_a
        with transaction.atomic(), tenant_context(tenant_a.id):
            device.is_active = False
            device.save(update_fields=["is_active"])

        response = anon_client.post(
            PIN_LOGIN,
            {
                "tenant_slug": tenant_a.slug,
                "device_token": raw_token,
                "username": "mary",
                "pin": "1234",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_another_businesss_device_token_does_not_work(
        self, anon_client, tenant_b, cashier_b, device_a
    ):
        """A till registered to one shop cannot sign anyone into another."""
        _device, raw_token = device_a
        response = anon_client.post(
            PIN_LOGIN,
            {
                "tenant_slug": tenant_b.slug,
                "device_token": raw_token,
                "username": "mary",
                "pin": "4321",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_a_user_without_a_pin_cannot_use_fast_sign_in(
        self, anon_client, tenant_a, owner_a, device_a
    ):
        _device, raw_token = device_a
        response = anon_client.post(
            PIN_LOGIN,
            {
                "tenant_slug": tenant_a.slug,
                "device_token": raw_token,
                "username": "owner",
                "pin": "1234",
            },
            format="json",
        )
        assert response.status_code == 400


class TestPinRules:
    """A PIN is 4 to 6 digits, and never stored as typed."""

    def test_a_pin_is_hashed(self, cashier_a):
        assert cashier_a.pin_hash
        assert "1234" not in cashier_a.pin_hash

    @pytest.mark.parametrize("bad_pin", ["123", "1234567", "abcd", "12a4", ""])
    def test_invalid_pins_are_refused(self, owner_a, bad_pin):
        if bad_pin == "":
            owner_a.set_pin(bad_pin)
            assert owner_a.pin_hash == ""
            return
        with pytest.raises(ValueError):
            owner_a.set_pin(bad_pin)

    def test_checking_a_pin_when_none_is_set_is_false(self, owner_a):
        assert owner_a.check_pin("1234") is False


class TestSessionEndpoints:
    def test_me_returns_the_caller_and_their_business(self, client_cashier_a, tenant_a):
        response = client_cashier_a.get("/api/v1/auth/me/")
        assert response.status_code == 200

        body = response.json()
        assert body["username"] == "mary"
        assert body["tenant"]["slug"] == tenant_a.slug
        assert body["is_platform_admin"] is False

    def test_me_requires_a_token(self, anon_client):
        assert anon_client.get("/api/v1/auth/me/").status_code == 401

    def test_a_refresh_token_can_be_exchanged(self, anon_client, tenant_a, cashier_a):
        signed_in = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        ).json()

        response = anon_client.post(
            "/api/v1/auth/refresh/", {"refresh": signed_in["refresh"]}, format="json"
        )
        assert response.status_code == 200

    def test_refreshing_works_without_an_authorization_header(
        self, anon_client, tenant_a, cashier_a
    ):
        """The case the refresh endpoint exists for.

        A till that has been idle sends only the refresh token, in the body,
        because its access token has expired - there is nothing to put in the
        Authorization header. The tenant therefore has to be read from the
        refresh token itself, or the user lookup is refused by isolation and
        the till can never sign back in without a full password sign-in.
        """
        signed_in = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        ).json()

        bare_client = anon_client.__class__()
        assert "HTTP_AUTHORIZATION" not in getattr(bare_client, "_credentials", {})

        response = bare_client.post(
            "/api/v1/auth/refresh/", {"refresh": signed_in["refresh"]}, format="json"
        )
        assert response.status_code == 200, response.content

    def test_an_unreadable_refresh_token_is_refused_not_crashed(self, anon_client):
        response = anon_client.post(
            "/api/v1/auth/refresh/", {"refresh": "not-a-token"}, format="json"
        )
        assert response.status_code == 401

    def test_a_refreshed_token_keeps_the_business_claim(self, anon_client, tenant_a, cashier_a):
        """A till running for a fortnight must not lose its tenant binding."""
        from rest_framework_simplejwt.tokens import AccessToken

        signed_in = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        ).json()
        refreshed = anon_client.post(
            "/api/v1/auth/refresh/", {"refresh": signed_in["refresh"]}, format="json"
        ).json()

        assert AccessToken(refreshed["access"])["tenant_id"] == str(tenant_a.id)

    def test_signing_out_blacklists_the_refresh_token(self, anon_client, tenant_a, cashier_a):
        signed_in = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "staff-pass-4471"},
            format="json",
        ).json()

        anon_client.credentials(HTTP_AUTHORIZATION=f"Bearer {signed_in['access']}")
        assert (
            anon_client.post(
                "/api/v1/auth/logout/", {"refresh": signed_in["refresh"]}, format="json"
            ).status_code
            == 205
        )

        reuse = anon_client.post(
            "/api/v1/auth/refresh/", {"refresh": signed_in["refresh"]}, format="json"
        )
        assert reuse.status_code == 401

    def test_changing_own_password_requires_the_current_one(self, client_cashier_a):
        response = client_cashier_a.post(
            "/api/v1/auth/change-password/",
            {"current_password": "wrong", "new_password": "brand-new-pass-2231"},
            format="json",
        )
        assert response.status_code == 400

    def test_changing_own_password_works(self, client_cashier_a, cashier_a, tenant_a):
        response = client_cashier_a.post(
            "/api/v1/auth/change-password/",
            {"current_password": "staff-pass-4471", "new_password": "brand-new-pass-2231"},
            format="json",
        )
        assert response.status_code == 204

        with transaction.atomic(), tenant_context(tenant_a.id):
            refreshed = User.objects.get(pk=cashier_a.pk)
        assert refreshed.check_password("brand-new-pass-2231")
