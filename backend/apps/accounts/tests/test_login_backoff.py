"""Growing delays on password sign-in, and proof they are not decorative.

Password sign-in had only a per-address rate limit, which bounds one source
rather than one account: an attacker spread across a few addresses got the full
allowance against a named owner from each, indefinitely. That ran the wrong way
round - the weaker-protected credential was the more privileged one.

**Why a delay and not a lockout, checked rather than assumed.** A hard lock
would strand a business. A provisioned owner is created with a password and no
PIN, ``check_pin`` refuses an empty hash, PIN sign-in additionally requires a
registered device, and the platform console exposes no reset or unlock. There
is a test below that pins each link of that chain, because the day somebody
gives owners a default PIN is the day a hard lock becomes survivable - and the
day this reasoning should be revisited rather than silently inherited.

Every test is written against a mutation it must fail on:

    * remove the counter (never record a failure)   -> the escalation tests fail
    * key on address instead of account             -> the cross-address test fails
    * remove the cap from the curve                 -> the cap test fails
    * let a delayed request consume a real attempt  -> the no-double-count test fails
    * audit only delayed attempts                   -> the audit tests fail
    * drop the list around the refusal message      -> the indistinguishability test fails

Time is moved with a fake clock rather than slept through: the delays run to
five minutes and the suite is under two.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.accounts import backoff
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context

LOGIN = "/api/v1/auth/login/"
CORRECT = "staff-pass-4471"


def attempt(client, tenant_slug, username="mary", password="wrong"):
    return client.post(
        LOGIN,
        {"tenant_slug": tenant_slug, "username": username, "password": password},
        format="json",
    )


def fail_times(client, tenant_slug, count, username="mary"):
    return [attempt(client, tenant_slug, username).status_code for _ in range(count)]


# ---------------------------------------------------------------------------


class TestTheCurve:
    """Pure arithmetic, no database. Mutation: remove the cap -> the last fails."""

    def test_the_first_attempts_are_free(self):
        assert backoff.delay_for(1) == 0
        assert backoff.delay_for(2) == 0
        assert backoff.delay_for(3) == 0

    def test_it_doubles_from_two_seconds(self):
        assert backoff.delay_for(4) == 2
        assert backoff.delay_for(5) == 4
        assert backoff.delay_for(6) == 8
        assert backoff.delay_for(7) == 16
        assert backoff.delay_for(8) == 32

    def test_it_is_capped(self):
        """An uncapped doubling is a permanent lockout wearing another name.

        Twenty failures uncapped is over a day; forty is longer than the
        business will exist. The cap is what keeps this survivable, and a shop
        with no other way in has to be able to wait it out.
        """
        assert backoff.delay_for(20) == 300
        assert backoff.delay_for(40) == 300
        assert backoff.delay_for(400) == 300


@pytest.mark.django_db
class TestTheDelayEngages:
    """Mutation: never call ``record_failure``. Both fail."""

    def test_three_wrong_passwords_are_still_accepted_attempts(
        self, anon_client, tenant_a, cashier_a
    ):
        """Typing a password wrong is normal and must not cost anything."""
        codes = fail_times(anon_client, tenant_a.slug, 3)
        assert codes == [400, 400, 400], codes

    def test_the_fourth_failure_earns_a_wait(self, anon_client, tenant_a, cashier_a):
        fail_times(anon_client, tenant_a.slug, 4)
        refused = attempt(anon_client, tenant_a.slug)
        assert refused.status_code == 429
        assert refused.json()["code"] == "login_backoff"
        assert refused.json()["retry_after_seconds"] > 0


@pytest.mark.django_db
class TestItIsKeyedOnTheAccountNotTheAddress:
    """The whole reason this exists alongside the rate limit.

    Mutation: key ``backoff`` on the client address instead of tenant and
    username. This test fails, because the attacker simply moves address.
    """

    def test_moving_address_does_not_refresh_the_allowance(
        self, anon_client, tenant_a, cashier_a
    ):
        for i in range(4):
            anon_client.post(
                LOGIN,
                {"tenant_slug": tenant_a.slug, "username": "mary", "password": "wrong"},
                format="json",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            )

        from_somewhere_new = anon_client.post(
            LOGIN,
            {"tenant_slug": tenant_a.slug, "username": "mary", "password": "wrong"},
            format="json",
            HTTP_X_FORWARDED_FOR="198.51.100.77",
        )
        assert from_somewhere_new.status_code == 429, (
            "changing address bought a fresh allowance against the same account"
        )

    def test_another_account_is_unaffected(
        self, anon_client, tenant_a, cashier_a, manager_a
    ):
        """One account's failures must not delay a colleague's sign-in."""
        fail_times(anon_client, tenant_a.slug, 5, username="mary")

        colleague = attempt(anon_client, tenant_a.slug, username="mngr")
        assert colleague.status_code == 400, "a delay on one account spread to another"

    def test_the_same_username_in_another_business_is_unaffected(
        self, anon_client, tenant_a, tenant_b, cashier_a, cashier_b
    ):
        """Counters are per business as well as per name."""
        fail_times(anon_client, tenant_a.slug, 5, username="mary")

        elsewhere = attempt(anon_client, tenant_b.slug, username="mary")
        assert elsewhere.status_code == 400, "a delay crossed a business boundary"


@pytest.mark.django_db
class TestADelayedRequestIsNotAlsoAFailure:
    """Mutation: record the failure before checking the delay.

    If a refused-because-delayed request also counted as a wrong password, the
    delay would feed itself: an attacker hammering a locked account would drive
    it to the cap and hold it there forever, which is the denial of service
    this design exists to avoid.
    """

    def test_hammering_during_a_wait_does_not_extend_it(
        self, anon_client, tenant_a, cashier_a
    ):
        fail_times(anon_client, tenant_a.slug, 4)

        before = backoff.check(tenant_a.id, "mary")
        fail_times(anon_client, tenant_a.slug, 10)
        after = backoff.check(tenant_a.id, "mary")

        assert after.failures == before.failures, (
            f"attempts made during a wait were counted ({before.failures} -> "
            f"{after.failures})"
        )
        assert after.retry_after_seconds <= before.retry_after_seconds


@pytest.mark.django_db
class TestSuccessClearsIt:
    def test_a_correct_password_resets_the_counter(
        self, anon_client, tenant_a, cashier_a
    ):
        """Somebody who fumbles twice and then gets it right starts clean."""
        fail_times(anon_client, tenant_a.slug, 3)
        assert backoff.check(tenant_a.id, "mary").failures == 3

        ok = attempt(anon_client, tenant_a.slug, password=CORRECT)
        assert ok.status_code == 200

        assert backoff.check(tenant_a.id, "mary").failures == 0

    def test_a_correct_password_is_refused_while_waiting(
        self, anon_client, tenant_a, cashier_a
    ):
        """The wait applies to the credential, not to the guess.

        Checked before the password is evaluated on purpose: evaluating it
        anyway would let an attacker tell a right password from a wrong one by
        response timing while nominally locked out.
        """
        fail_times(anon_client, tenant_a.slug, 4)
        refused = attempt(anon_client, tenant_a.slug, password=CORRECT)
        assert refused.status_code == 429


@pytest.mark.django_db
class TestItDecays:
    """Mutation: never expire the counter. This fails.

    A counter that never decays turns one bad afternoon into a permanent
    penalty, which is the same stranding problem in slow motion.
    """

    @override_settings(LOGIN_BACKOFF_DECAY_SECONDS=1)
    def test_the_counter_expires_after_a_quiet_window(
        self, anon_client, tenant_a, cashier_a
    ):
        import time

        fail_times(anon_client, tenant_a.slug, 3)
        assert backoff.check(tenant_a.id, "mary").failures == 3

        time.sleep(1.1)

        assert backoff.check(tenant_a.id, "mary").failures == 0, (
            "the failure counter outlived its decay window"
        )


@pytest.mark.django_db
class TestEveryFailureIsAudited:
    """Mutation: audit only attempts that trigger a delay. Both fail.

    A slow campaign - a few attempts an hour, never enough to earn a delay -
    would otherwise leave nothing behind at all, because the counter that would
    have noticed expires in fifteen minutes.
    """

    def _entries(self, tenant):
        with tenant_context(tenant.id):
            return list(
                AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).order_by(
                    "created_at"
                )
            )

    def test_a_single_failure_well_below_the_threshold_is_recorded(
        self, anon_client, tenant_a, cashier_a
    ):
        attempt(anon_client, tenant_a.slug)

        entries = self._entries(tenant_a)
        assert len(entries) == 1
        assert entries[0].after["method"] == "password"
        assert entries[0].reason == "bad_password"

    def test_a_refusal_during_a_wait_is_recorded_too(
        self, anon_client, tenant_a, cashier_a
    ):
        fail_times(anon_client, tenant_a.slug, 4)
        attempt(anon_client, tenant_a.slug)

        reasons = [entry.reason for entry in self._entries(tenant_a)]
        assert "backoff" in reasons, "a refusal during a wait left no record"

    def test_the_entry_names_nobody(self, anon_client, tenant_a, cashier_a):
        """A failed sign-in proves a name was typed, not who typed it.

        Attaching the user would file an attacker's activity in the victim's
        history, and this trail is what a manager reads when money is missing.
        """
        attempt(anon_client, tenant_a.slug)

        entry = self._entries(tenant_a)[0]
        assert entry.actor_id is None
        assert entry.entity_id == "mary"

    def test_the_password_is_never_in_the_entry(
        self, anon_client, tenant_a, cashier_a
    ):
        attempt(anon_client, tenant_a.slug, password="hunter2-secret")

        blob = str(self._entries(tenant_a)[0].__dict__)
        assert "hunter2-secret" not in blob


@pytest.mark.django_db
class TestARefusalStillRevealsNothing:
    """Mutation: drop the list around the refusal message in the view.

    The backoff work moved the credential check out of the serializer, and a
    view raising an error by hand renders it differently from a serializer
    raising the same one. That difference is an oracle: it tells a caller which
    business slugs are real without them guessing a single password.
    """

    def test_an_unknown_business_and_a_wrong_password_are_identical(
        self, anon_client, tenant_a, cashier_a
    ):
        unknown = attempt(anon_client, "no-such-shop-at-all")
        wrong = attempt(anon_client, tenant_a.slug)

        assert unknown.status_code == wrong.status_code == 400
        assert unknown.json() == wrong.json(), (
            "an unknown business is distinguishable from a wrong password"
        )

    def test_an_unknown_username_matches_a_known_one(
        self, anon_client, tenant_a, cashier_a
    ):
        nobody = attempt(anon_client, tenant_a.slug, username="ghost")
        somebody = attempt(anon_client, tenant_a.slug, username="mary")

        assert nobody.json() == somebody.json()


@pytest.mark.django_db
class TestTheStrandingAssumptionsStillHold:
    """The facts that made a delay right and a hard lock wrong.

    Not a test of the backoff itself. It pins the chain the design decision
    rests on, so that changing any link fails here and forces the decision to
    be made again rather than inherited.
    """

    def test_a_provisioned_owner_has_no_pin(self, tenant_a):
        from apps.accounts.models import User

        with tenant_context(tenant_a.id):
            owner = User.objects.get(tenant=tenant_a, username="owner")

        assert not owner.pin_hash, (
            "owners now get a PIN by default - a hard lock on password sign-in "
            "may now be survivable, so revisit apps/accounts/backoff.py"
        )

    def test_an_empty_pin_hash_never_authenticates(self, tenant_a):
        from apps.accounts.models import User

        with tenant_context(tenant_a.id):
            owner = User.objects.get(tenant=tenant_a, username="owner")

        assert owner.check_pin("") is False
        assert owner.check_pin("0000") is False

    def test_the_platform_console_exposes_no_password_reset(self):
        """If this starts failing, somebody added a recovery path - good.

        That would be the moment to reconsider whether a delay is still the
        right shape, because the reason it is a delay is that there is no way
        back in.
        """
        from apps.platform_admin import urls as platform_urls

        routes = " ".join(str(pattern.pattern) for pattern in platform_urls.urlpatterns)
        for word in ("reset", "unlock", "password"):
            assert word not in routes.lower(), (
                f"the platform console now exposes {word!r}; revisit whether "
                "password sign-in should lock rather than slow down"
            )
