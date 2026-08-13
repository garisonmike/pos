"""
Till lockout on repeated PIN failures.

The property under test is the one a rate limit cannot give: a bounded *total*
number of attempts, not merely a bounded rate. An attacker holding a stolen
till and willing to wait should still run out of tries.
"""

from __future__ import annotations

import pytest
from django.db import transaction
from django.test import override_settings

from apps.accounts import lockout
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db

PIN_LOGIN = "/api/v1/auth/pin-login/"


def attempt(client, tenant, token: str, username: str = "mary", pin: str = "0000"):
    """One PIN sign-in attempt."""
    return client.post(
        PIN_LOGIN,
        {
            "tenant_slug": tenant.slug,
            "device_token": token,
            "username": username,
            "pin": pin,
        },
        format="json",
    )


class TestLockout:
    def test_a_wrong_pin_reports_attempts_remaining(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        """The till can warn a cashier before it locks them out."""
        _device, token = device_a
        response = attempt(anon_client, tenant_a, token)

        assert response.status_code == 400
        assert response.json()["attempts_remaining"] == 4

    def test_the_device_locks_after_the_configured_failures(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        _device, token = device_a
        for _ in range(4):
            assert attempt(anon_client, tenant_a, token).status_code == 400

        fifth = attempt(anon_client, tenant_a, token)
        assert fifth.status_code == 429
        assert fifth.json()["code"] == "pin_locked_out"
        assert fifth.json()["retry_after_seconds"] > 0

    def test_the_correct_pin_is_refused_while_locked(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        """The point of a lockout: being right afterwards does not help.

        If the right PIN still worked once the limit was hit, an attacker
        walking the space would simply succeed on the attempt that happened to
        be correct, and the lockout would stop nothing.
        """
        _device, token = device_a
        for _ in range(5):
            attempt(anon_client, tenant_a, token)

        response = attempt(anon_client, tenant_a, token, pin="1234")
        assert response.status_code == 429

    def test_a_successful_sign_in_clears_the_count(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        """A cashier who fumbles twice and then succeeds starts clean."""
        _device, token = device_a
        attempt(anon_client, tenant_a, token)
        attempt(anon_client, tenant_a, token)

        assert attempt(anon_client, tenant_a, token, pin="1234").status_code == 200

        after = attempt(anon_client, tenant_a, token)
        assert after.status_code == 400
        assert after.json()["attempts_remaining"] == 4

    def test_an_unregistered_device_does_not_count_towards_lockout(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        """A wrong token is a different failure from a wrong PIN.

        Counting it would let anyone lock out a till they cannot otherwise
        touch, by sending rubbish tokens - turning the protection into a way to
        stop a shop trading.
        """
        _device, token = device_a
        for _ in range(8):
            response = attempt(anon_client, tenant_a, "not-a-real-token")
            assert response.status_code == 400
            assert response.json()["code"] == "device_not_registered"

        assert attempt(anon_client, tenant_a, token, pin="1234").status_code == 200

    def test_lockout_is_scoped_to_one_device(
        self, anon_client, tenant_a, cashier_a, device_a, client_owner_a
    ):
        """Locking one till must not stop the shop trading on another.

        A different cashier is used on the second till on purpose. The two
        counters are independent dimensions: locking the *device* must not
        affect another device, and locking a *user* is separately expected to
        follow them everywhere - see the test below. Re-using one cashier here
        would exercise the second rule while claiming to test the first.
        """
        _device, token = device_a
        client_owner_a.post(
            "/api/v1/auth/users/",
            {
                "username": "peter",
                "full_name": "Peter Omondi",
                "password": "peter-pass-8823",
                "role": "CASHIER",
                "pin": "5678",
            },
            format="json",
        )
        second = client_owner_a.post(
            "/api/v1/auth/devices/", {"name": "Second counter"}, format="json"
        ).json()["device_token"]

        for _ in range(5):
            attempt(anon_client, tenant_a, token)
        assert attempt(anon_client, tenant_a, token).status_code == 429

        assert (
            attempt(
                anon_client, tenant_a, second, username="peter", pin="5678"
            ).status_code
            == 200
        )

    def test_locking_a_user_does_follow_them_to_another_device(
        self, anon_client, tenant_a, cashier_a, device_a, client_owner_a
    ):
        """The other half of the pair, stated explicitly.

        Once a cashier's PIN has been guessed at enough times, moving to a
        different till must not reset the count - otherwise the per-device
        limit is trivially sidestepped by walking down the counter.
        """
        _device, token = device_a
        second = client_owner_a.post(
            "/api/v1/auth/devices/", {"name": "Second counter"}, format="json"
        ).json()["device_token"]

        for _ in range(5):
            attempt(anon_client, tenant_a, token)

        assert attempt(anon_client, tenant_a, second, pin="1234").status_code == 429

    def test_lockout_is_scoped_to_one_business(
        self, anon_client, tenant_a, tenant_b, cashier_a, cashier_b, device_a
    ):
        """One business exhausting attempts must not affect another."""
        _device, token = device_a
        for _ in range(6):
            attempt(anon_client, tenant_a, token)

        state = lockout.check(tenant_b.id, "some-other-device", "mary")
        assert state.is_locked is False

    def test_one_user_cannot_be_ground_down_across_devices(
        self, anon_client, tenant_a, cashier_a, device_a, client_manager_a
    ):
        """The second counter, which a per-device limit alone would miss.

        Spreading attempts across tills keeps every device counter low, so
        without a per-user counter the total number of guesses against one
        cashier would be unbounded.
        """
        _device, first = device_a
        tokens = [first]
        for index in range(4):
            tokens.append(
                client_manager_a.post(
                    "/api/v1/auth/devices/", {"name": f"Till {index}"}, format="json"
                ).json()["device_token"]
            )

        statuses = [attempt(anon_client, tenant_a, token).status_code for token in tokens]

        assert 429 in statuses, (
            "Attempts spread across five different tills never tripped a limit, "
            "so a single cashier's PIN can be guessed without bound."
        )

    @override_settings(PIN_LOCKOUT_MAX_ATTEMPTS=2)
    def test_the_threshold_is_configurable(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        _device, token = device_a
        assert attempt(anon_client, tenant_a, token).status_code == 400
        assert attempt(anon_client, tenant_a, token).status_code == 429


class TestFailuresAreAudited:
    """The cache decides the next attempt; the audit trail is the record."""

    def test_a_failed_attempt_is_written_to_the_audit_trail(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        _device, token = device_a
        attempt(anon_client, tenant_a, token)

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).first()

        assert entry is not None
        assert entry.entity_id == "mary"
        assert entry.reason == "pin_refused"
        assert entry.after["method"] == "pin"

    def test_the_lockout_itself_is_recorded(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        """A manager investigating a missing float needs to see this.

        The counters expire in fifteen minutes; someone sitting at the counter
        trying PINs at eleven at night should still be visible tomorrow.
        """
        _device, token = device_a
        for _ in range(6):
            attempt(anon_client, tenant_a, token)

        with transaction.atomic(), tenant_context(tenant_a.id):
            reasons = list(
                AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).values_list(
                    "reason", flat=True
                )
            )

        assert reasons.count("pin_refused") == 5
        assert "locked_out" in reasons

    def test_a_failed_attempt_is_not_filed_against_the_cashier(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        """Nobody has proved they are that cashier yet.

        Attaching the user would put someone else's guessing into an innocent
        person's history, which is exactly the record a manager would later
        read as evidence against them.
        """
        _device, token = device_a
        attempt(anon_client, tenant_a, token)

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).first()

        assert entry.actor_id is None
        assert entry.actor_label == ""

    def test_the_audit_entry_never_contains_the_pin(
        self, anon_client, tenant_a, cashier_a, device_a
    ):
        _device, token = device_a
        attempt(anon_client, tenant_a, token, pin="9182")

        with transaction.atomic(), tenant_context(tenant_a.id):
            entries = list(AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED))

        assert "9182" not in str([(e.before, e.after, e.reason) for e in entries])

    def test_failures_are_visible_only_to_their_own_business(
        self, anon_client, tenant_a, tenant_b, cashier_a, device_a
    ):
        """Sign-in failures are business data like anything else."""
        _device, token = device_a
        attempt(anon_client, tenant_a, token)

        with transaction.atomic(), tenant_context(tenant_b.id):
            assert not AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).exists()


class TestLockoutHelpers:
    """The module's own contract, independent of the view."""

    def test_check_reports_not_locked_when_nothing_has_failed(self, tenant_a):
        assert lockout.check(tenant_a.id, "device-1", "mary").is_locked is False

    def test_retry_after_is_never_reported_as_zero_minutes(self):
        """Telling a cashier to wait zero minutes is worse than useless."""
        state = lockout.LockoutState(is_locked=True, retry_after_seconds=20)
        assert state.retry_after_minutes == 1

    def test_clearing_forgets_previous_failures(self, tenant_a):
        for _ in range(4):
            lockout.record_failure(tenant_a.id, "device-1", "mary")
        assert lockout.check(tenant_a.id, "device-1", "mary").attempts == 4

        lockout.clear(tenant_a.id, "device-1", "mary")
        assert lockout.check(tenant_a.id, "device-1", "mary").attempts == 0
