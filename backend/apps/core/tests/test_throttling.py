"""The sign-in rate limits, and proof they are not decorative.

These exist because the limits were shipped unverified and did not work. DRF
resolves the client address through its own ``NUM_PROXIES`` setting rather than
this project's ``TRUSTED_PROXY_HOPS``; unset, it used the whole
``X-Forwarded-For`` header as the throttle key, so a caller changing one header
per request got a fresh allowance every time.

Every test here is written against a mutation it must fail on. The mutations
are recorded in the class docstrings so the next person can re-run them rather
than trust that these once caught something:

    * revert ``get_ident`` to DRF's default          -> 6 fail
    * hardcode ``index = 0`` in ``client_ip``        -> 3 fail
    * drop the device from the pin-login key         -> 1 fails
    * give an unresolved address a unique value      -> 1 fails
    * collapse both scopes onto one                  -> 6 fail
    * remove ``throttle_scope`` from both views      -> 12 fail

Recorded because one of them did *not* work first time. Returning ``None`` for
an unresolved address killed nothing: ``ScopedRateThrottle`` formats its cache
key unconditionally, so None becomes the string "None" and is still counted.
The mutation that does kill it is a unique value per request, which is the
plausible mistake, and that is what the test is written against.

The rates are lowered per test rather than waited out: throttle windows are a
minute and the suite is a hundred seconds.
"""

from __future__ import annotations

import pytest

LOGIN_URL = "/api/v1/auth/login/"
PIN_URL = "/api/v1/auth/pin-login/"

THROTTLED = 429


@pytest.fixture(autouse=True)
def one_proxy_in_front(settings):
    """Speak to the application the way the deployment does.

    Test settings default ``TRUSTED_PROXY_HOPS`` to 0, which means the socket
    address is used and ``X-Forwarded-For`` is ignored entirely. Under the test
    client every request then arrives from 127.0.0.1, so every test in this
    file would share one bucket and several would pass without the header ever
    being read. Tests that care about a different hop count set it themselves;
    an autouse fixture runs first, so the body wins.
    """
    settings.TRUSTED_PROXY_HOPS = 1


@pytest.fixture
def rates(monkeypatch):
    """Set the sign-in rates for one test.

    Patches the throttle class rather than the Django setting. Going through
    ``settings.REST_FRAMEWORK`` plus ``api_settings.reload()`` does not survive
    teardown ordering: ``reload()`` runs while the test's settings are still in
    place, so DRF caches that test's rates and the *next* test silently runs
    with them. That produced a test which passed alone and failed in the file -
    the throttle appeared not to engage, when in fact its rate was None.

    ``monkeypatch`` restores the attribute itself, so there is no ordering to
    get wrong.
    """
    from apps.core.throttling import ClientAddressScopedThrottle

    def _apply(**scopes):
        # Any scope not named is switched off rather than left unset: DRF
        # raises ImproperlyConfigured for a scope with no rate, so an omitted
        # one would fail the test for a reason unrelated to what it asserts.
        monkeypatch.setattr(
            ClientAddressScopedThrottle,
            "THROTTLE_RATES",
            {"login": None, "pin-login": None, **scopes},
            raising=False,
        )

    return _apply


def sign_in(client, url=LOGIN_URL, address=None, device=None, **extra):
    """One sign-in attempt with deliberately wrong credentials.

    Wrong on purpose: a throttle is checked before authentication, so a refused
    attempt consumes the allowance exactly as a successful one would. That is
    the property being measured.
    """
    headers = {}
    if address is not None:
        headers["HTTP_X_FORWARDED_FOR"] = address

    body = {"slug": "nowhere", "username": "nobody", "password": "wrong"}
    if url == PIN_URL:
        body = {
            "tenant_slug": "nowhere",
            "username": "nobody",
            "pin": "0000",
            "device_token": device or "device-token-a",
        }

    return client.post(url, {**body, **extra}, **headers)


def statuses(client, count, **kwargs):
    return [sign_in(client, **kwargs).status_code for _ in range(count)]


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheLimitEngagesAtAll:
    """Mutation: remove ``throttle_scope`` from the view. Both fail."""

    def test_a_password_sign_in_is_throttled(self, client, rates):
        rates(login="5/min")
        codes = statuses(client, 9, address="203.0.113.9")
        assert THROTTLED in codes
        assert codes.count(THROTTLED) == 4, codes

    def test_a_pin_sign_in_is_throttled(self, client, rates):
        rates(**{"pin-login": "5/min"})
        codes = statuses(client, 9, url=PIN_URL, address="203.0.113.9")
        assert THROTTLED in codes
        assert codes.count(THROTTLED) == 4, codes


@pytest.mark.django_db
class TestAForgedHeaderDoesNotBuyAFreshAllowance:
    """The bug this module was written for.

    Mutation: revert ``ClientAddressScopedThrottle.get_ident`` to DRF's
    inherited one. Every test in this class fails - which is exactly what the
    original probe measured before the fix existed.
    """

    def test_a_varying_leading_entry_is_still_throttled(self, client, rates):
        rates(login="5/min")
        # One trusted proxy: our own appends the real peer on the right, and
        # everything to the left is whatever the caller chose to send.
        codes = [
            sign_in(client, address=f"10.0.0.{i}, 203.0.113.9").status_code
            for i in range(12)
        ]
        assert THROTTLED in codes, (
            "a caller varying X-Forwarded-For got a fresh bucket every request"
        )
        assert codes.count(THROTTLED) == 7, codes

    def test_a_lengthening_forged_chain_is_still_throttled(self, client, rates):
        rates(login="5/min")
        codes = [
            sign_in(
                client, address=", ".join(f"10.0.0.{n}" for n in range(i + 1)) + ", 203.0.113.9"
            ).status_code
            for i in range(12)
        ]
        assert codes.count(THROTTLED) == 7, codes

    def test_two_genuinely_different_addresses_keep_their_own_allowance(
        self, client, rates
    ):
        """The limit must not be so blunt that one shop throttles another."""
        rates(login="5/min")
        first = statuses(client, 5, address="203.0.113.9")
        second = statuses(client, 5, address="198.51.100.4")
        assert THROTTLED not in first
        assert THROTTLED not in second, "a second address inherited the first's count"


@pytest.mark.django_db
class TestTheAddressIsReadTheSameWayEverywhereElse:
    """Mutation: hardcode ``index = 0`` in ``apps.core.net.client_ip``.

    That is the same mutation the M-Pesa allowlist tests are proved against, so
    the throttle and the allowlist cannot disagree about who is calling.
    """

    def test_with_two_trusted_proxies_the_second_from_last_is_counted(
        self, client, rates, settings
    ):
        settings.TRUSTED_PROXY_HOPS = 2
        rates(login="5/min")
        # Real peer is the middle entry; the rightmost is our second proxy.
        codes = [
            sign_in(client, address=f"10.0.0.{i}, 203.0.113.9, 172.16.0.1").status_code
            for i in range(12)
        ]
        assert codes.count(THROTTLED) == 7, codes

    def test_with_no_proxy_the_socket_address_is_counted(self, client, rates, settings):
        settings.TRUSTED_PROXY_HOPS = 0
        rates(login="5/min")
        # The header is present and varying, and must be ignored entirely.
        codes = [sign_in(client, address=f"10.0.0.{i}").status_code for i in range(12)]
        assert codes.count(THROTTLED) == 7, codes


@pytest.mark.django_db
class TestAnUnresolvableAddressFailsClosed:
    """Mutation: give each unresolved request a unique value in ``get_ident``.

    Not ``None`` - that was tried and killed nothing, because
    ``ScopedRateThrottle`` formats its key unconditionally and None becomes the
    string "None", still counted. A unique value per request is the mistake
    that actually opens the hole, and is what somebody would plausibly write
    while tidying that line.
    """

    def test_fewer_entries_than_hops_still_counts(self, client, rates, settings):
        settings.TRUSTED_PROXY_HOPS = 4
        rates(login="5/min")
        # client_ip refuses to guess and returns None here.
        codes = [sign_in(client, address=f"10.0.0.{i}").status_code for i in range(12)]
        assert THROTTLED in codes, "an unresolvable address escaped the limit entirely"


@pytest.mark.django_db
class TestPinSignInIsCountedPerTill:
    """Mutation: drop the device from the pin-login key.

    Every till in a shop shares one NAT address. An address-only limit lets one
    device exhaust the allowance for the whole building, and refusing sign-in
    mid-trade is its own kind of outage.
    """

    def test_one_till_running_out_does_not_stop_another(self, client, rates):
        rates(**{"pin-login": "5/min"})
        exhausted = statuses(
            client, 9, url=PIN_URL, address="203.0.113.9", device="till-one"
        )
        assert THROTTLED in exhausted

        neighbour = statuses(
            client, 5, url=PIN_URL, address="203.0.113.9", device="till-two"
        )
        assert THROTTLED not in neighbour, (
            "one till exhausting its allowance blocked another at the same address"
        )

    def test_the_same_till_is_still_limited(self, client, rates):
        rates(**{"pin-login": "5/min"})
        codes = statuses(
            client, 9, url=PIN_URL, address="203.0.113.9", device="till-one"
        )
        assert codes.count(THROTTLED) == 4, codes

    def test_a_missing_device_token_does_not_escape_the_limit(self, client, rates):
        """Mutation: return a random value for an unreadable device."""
        rates(**{"pin-login": "5/min"})
        codes = []
        for _ in range(12):
            response = client.post(
                PIN_URL,
                {"tenant_slug": "nowhere", "username": "nobody", "pin": "0000"},
                HTTP_X_FORWARDED_FOR="203.0.113.9",
            )
            codes.append(response.status_code)
        assert THROTTLED in codes, "a request with no device token was never counted"


@pytest.mark.django_db
class TestTheTwoScopesAreIndependent:
    """Mutation: give both views the same ``throttle_scope``.

    A cashier's PIN attempts must not use up an owner's ability to sign in with
    a password - that is the route back in when a till is locked out.
    """

    def test_exhausting_pin_sign_in_leaves_password_sign_in_alone(self, client, rates):
        rates(**{"pin-login": "5/min", "login": "5/min"})
        pin_codes = statuses(client, 9, url=PIN_URL, address="203.0.113.9")
        assert THROTTLED in pin_codes

        password_codes = statuses(client, 5, address="203.0.113.9")
        assert THROTTLED not in password_codes, (
            "PIN attempts consumed the password sign-in allowance"
        )


@pytest.mark.django_db
class TestThrottlingAndLockoutDoNotFeedEachOther:
    """Mutation: move the lockout increment above the throttle check.

    If a throttled request still counted as a failed PIN, anyone able to send
    traffic could burn a cashier's five attempts from outside and lock staff
    out of their own till - turning a rate limit into a denial-of-service tool.
    """

    def test_a_throttled_request_does_not_consume_a_lockout_attempt(
        self, client, rates, tenant_a, cashier_a, device_a
    ):
        from apps.accounts import lockout
        from apps.accounts.models import hash_device_token

        rates(**{"pin-login": "3/min"})
        _device, token = device_a
        device_key = hash_device_token(token)

        codes = statuses(
            client,
            9,
            url=PIN_URL,
            address="203.0.113.9",
            device=token,
            tenant_slug=tenant_a.slug,
            username=cashier_a.username,
        )
        assert THROTTLED in codes

        state = lockout.check(tenant_a.id, device_key, cashier_a.username)
        # Three attempts got through and were counted; the six that were
        # throttled never reached the view, so they must not appear here.
        assert state.attempts <= 3, (
            f"throttled requests were counted as failed PINs ({state.attempts})"
        )
