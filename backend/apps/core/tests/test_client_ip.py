"""
Which X-Forwarded-For entry to believe.

This decides whether a forged M-Pesa callback gets credited, so the tests are
written from the attacker's side: what can someone put in that header, and does
it get them anything.
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory, override_settings

from apps.core.checks import check_trusted_proxy_hops
from apps.core.net import client_ip, is_ip_allowed

SAFARICOM = "196.201.214.200"
ATTACKER = "203.0.113.77"


def request_with(forwarded: str | None = None, remote: str = ATTACKER):
    factory = RequestFactory()
    headers = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        headers["HTTP_X_FORWARDED_FOR"] = forwarded
    return factory.post("/api/v1/payments/mpesa/callback/x/", **headers)


class TestWithNoProxy:
    def test_the_socket_address_is_used(self):
        assert client_ip(request_with(remote="10.0.0.5"), trusted_hops=0) == "10.0.0.5"

    def test_the_header_is_ignored_entirely(self):
        """With nothing in front, the header is pure client input."""
        request = request_with(forwarded=SAFARICOM, remote="10.0.0.5")
        assert client_ip(request, trusted_hops=0) == "10.0.0.5"


class TestWithOneTrustedProxy:
    def test_the_last_entry_is_believed(self):
        """Each proxy appends what it saw, so the last is ours."""
        request = request_with(forwarded=f"{ATTACKER}, {SAFARICOM}")
        assert client_ip(request, trusted_hops=1) == SAFARICOM

    def test_a_single_entry_is_the_one_our_proxy_added(self):
        request = request_with(forwarded=SAFARICOM)
        assert client_ip(request, trusted_hops=1) == SAFARICOM

    def test_a_spoofed_leading_entry_is_not_believed(self):
        """The whole reason this is not the first entry.

        Reading the first - the common default - would let anyone claim to be
        Safaricom by typing their address into a header they control.
        """
        request = request_with(forwarded=f"{SAFARICOM}, {ATTACKER}")
        assert client_ip(request, trusted_hops=1) == ATTACKER

    def test_a_long_forged_chain_gets_nowhere(self):
        forged = ", ".join([SAFARICOM] * 5) + f", {ATTACKER}"
        request = request_with(forwarded=forged)
        assert client_ip(request, trusted_hops=1) == ATTACKER


class TestWithTwoTrustedProxies:
    def test_it_counts_back_the_right_number_of_hops(self):
        request = request_with(forwarded=f"{ATTACKER}, {SAFARICOM}, 10.0.0.9")
        assert client_ip(request, trusted_hops=2) == SAFARICOM


class TestWhenTheHeaderIsWrong:
    def test_a_missing_header_falls_back_to_the_socket(self):
        assert client_ip(request_with(remote="10.0.0.5"), trusted_hops=1) == "10.0.0.5"

    def test_too_few_entries_yields_nothing_rather_than_a_guess(self):
        """The leftmost entry is the most attacker-controlled one.

        Returning it as a fallback would be the worst available answer, so this
        returns nothing and lets the allowlist fail closed.
        """
        request = request_with(forwarded=SAFARICOM)
        assert client_ip(request, trusted_hops=3) is None


class TestTheAllowlist:
    def test_an_address_on_the_list_passes(self):
        request = request_with(forwarded=f"{ATTACKER}, {SAFARICOM}")
        assert is_ip_allowed(request, [SAFARICOM], trusted_hops=1) is True

    def test_an_address_not_on_the_list_is_refused(self):
        request = request_with(forwarded=f"{SAFARICOM}, {ATTACKER}")
        assert is_ip_allowed(request, [SAFARICOM], trusted_hops=1) is False

    def test_an_empty_list_fails_closed(self):
        """Not configured must not look the same as configured correctly."""
        request = request_with(forwarded=SAFARICOM)
        assert is_ip_allowed(request, [], trusted_hops=1) is False

    def test_an_unresolvable_address_fails_closed(self):
        request = request_with(forwarded=SAFARICOM)
        assert is_ip_allowed(request, [SAFARICOM], trusted_hops=5) is False


class TestTheStartupCheck:
    @override_settings(DEBUG=False, TRUSTED_PROXY_HOPS=None)
    def test_production_refuses_to_run_without_a_hop_count(self):
        """There is no safe default.

        Too few and we read an entry the caller supplied, so a forged callback
        is credited. Too many and we read nothing, so every real callback is
        refused. Production has to say.
        """
        errors = check_trusted_proxy_hops(None)
        assert [error.id for error in errors] == ["pos.E004"]

    @override_settings(DEBUG=False, TRUSTED_PROXY_HOPS=None)
    def test_the_error_explains_what_to_set_it_to(self):
        hint = check_trusted_proxy_hops(None)[0].hint
        assert "1" in hint
        assert "X-Forwarded-For" in hint

    @override_settings(DEBUG=False, TRUSTED_PROXY_HOPS=1)
    def test_an_explicit_value_passes(self):
        assert check_trusted_proxy_hops(None) == []

    @override_settings(DEBUG=False, TRUSTED_PROXY_HOPS=0)
    def test_zero_is_an_explicit_answer_and_passes(self):
        """Zero means 'nothing is in front', which is a real answer."""
        assert check_trusted_proxy_hops(None) == []

    @override_settings(DEBUG=True, TRUSTED_PROXY_HOPS=None)
    def test_development_is_left_alone(self):
        assert check_trusted_proxy_hops(None) == []


@pytest.mark.django_db
class TestTheAuditTrailUsesTheSameRule:
    def test_a_recorded_address_is_the_trusted_one(self, tenant_a, owner_a, settings):
        """One rule for both, so a forensic record cannot say something the
        allowlist disagreed with."""
        from django.db import transaction

        from apps.core.audit import record_audit
        from apps.core.models import AuditAction, AuditLog
        from apps.core.tenancy import tenant_context

        settings.TRUSTED_PROXY_HOPS = 1
        request = request_with(forwarded=f"{SAFARICOM}, {ATTACKER}")

        with transaction.atomic(), tenant_context(tenant_a.id):
            record_audit(
                action=AuditAction.LOGIN,
                entity=owner_a,
                actor=owner_a,
                request=request,
            )
            entry = AuditLog.objects.first()

        assert entry.ip_address == ATTACKER
