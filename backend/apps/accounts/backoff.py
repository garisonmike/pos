"""Growing delays on password sign-in.

The sibling of ``apps.accounts.lockout``, for the other sign-in route and with
one deliberate difference: **this never locks, it only slows down.**

Why it is needed at all. PIN sign-in has two independent controls - a lockout
counting per device and per user, and a rate limit counting per till. Password
sign-in had only the rate limit, which counts per *address*. That bounds one
source, not one account: an attacker spread across a handful of addresses gets
the full per-address allowance against a named owner from each one, forever,
and nothing ever counts the pattern. The asymmetry ran the wrong way round -
the weaker-protected credential was the more privileged one, guarding
discounts, voids, refunds, M-Pesa credentials and every report.

Why it is a delay and not a lockout. A hard lock on an account is a
denial-of-service primitive: anyone who knows an owner's username could lock
them out of their own business mid-trade. That is not theoretical here, because
there is no way back in. A provisioned owner is created with a password and no
PIN (see ``apps.tenants.services.provision_tenant``), ``check_pin`` refuses an
empty PIN hash, PIN sign-in additionally requires a registered device, and the
platform console exposes no password reset or unlock endpoint. A locked-out
owner of a shop with no enrolled device would need database access to recover.

So the counter grows a waiting period instead, capped so that it always
expires:

    failures 1-3   no delay at all - typing a password wrong is normal
    4th            2 seconds
    5th            4s      6th   8s      7th   16s
    8th            32s     9th   64s     10th  128s
    11th onward    capped at 300s

At the cap that is twelve attempts an hour against one account, which makes
sustained guessing useless against anything with more entropy than a PIN, while
the worst an owner ever waits is five minutes. Counters clear on a successful
sign-in and decay on their own after 15 minutes with no attempts, so somebody
who gets it right on the fourth try is not carrying a penalty into next week.

The delay is enforced by *refusing* early with a retry hint, never by sleeping
the request. Sleeping would hold a gunicorn worker open for the duration and
turn the defence into the outage it is meant to prevent.

Cache-backed for the same reasons as the PIN lockout: failures are
high-frequency, worthless once expired, and must be visible to every worker at
once. The audit trail is the durable record; the cache only decides whether to
refuse the next attempt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

_COUNT_KEY = "pwfail:count:{tenant_id}:{username}"
_UNTIL_KEY = "pwfail:until:{tenant_id}:{username}"


@dataclass(frozen=True)
class BackoffState:
    """Whether an attempt is currently refused, and for how long."""

    is_delayed: bool
    retry_after_seconds: int = 0
    failures: int = 0


def _free_attempts() -> int:
    return getattr(settings, "LOGIN_BACKOFF_FREE_ATTEMPTS", 3)


def _base_seconds() -> int:
    return getattr(settings, "LOGIN_BACKOFF_BASE_SECONDS", 2)


def _max_seconds() -> int:
    return getattr(settings, "LOGIN_BACKOFF_MAX_SECONDS", 300)


def _decay_seconds() -> int:
    return getattr(settings, "LOGIN_BACKOFF_DECAY_SECONDS", 900)


def delay_for(failures: int) -> int:
    """The waiting period earned by this many consecutive failures.

    Capped deliberately and tested for it: an uncapped doubling reaches days
    within twenty attempts, which is a permanent lockout wearing a different
    name, and the stranding problem above makes that unacceptable.
    """
    free = _free_attempts()
    if failures <= free:
        return 0

    delay = _base_seconds() * (2 ** (failures - free - 1))
    return min(delay, _max_seconds())


def _keys(tenant_id, username: str) -> tuple[str, str]:
    # Lowercased so that varying the case of a username does not open a fresh
    # counter - usernames are matched case-sensitively at sign-in, but an
    # attacker does not have to respect that to get free attempts.
    handle = username.lower()
    return (
        _COUNT_KEY.format(tenant_id=tenant_id, username=handle),
        _UNTIL_KEY.format(tenant_id=tenant_id, username=handle),
    )


def check(tenant_id, username: str) -> BackoffState:
    """Report whether this account is inside a waiting period.

    Called before the password is evaluated, so a delayed attempt is refused
    without the credential being checked - the same reasoning as the PIN
    lockout, where evaluating it anyway would let an attacker tell "delayed"
    from "wrong password" by timing.
    """
    if not username:
        return BackoffState(is_delayed=False)

    count_key, until_key = _keys(tenant_id, username)
    failures = cache.get(count_key, 0)
    until = cache.get(until_key)

    if until is None:
        return BackoffState(is_delayed=False, failures=failures)

    remaining = int(until - time.time())
    if remaining <= 0:
        return BackoffState(is_delayed=False, failures=failures)

    return BackoffState(
        is_delayed=True, retry_after_seconds=remaining, failures=failures
    )


def record_failure(tenant_id, username: str) -> BackoffState:
    """Count a failed password attempt and report the resulting state.

    The decay window is refreshed on every failure rather than running from the
    first one, for the reason ``lockout.record_failure`` gives: an attacker
    pacing attempts to straddle a fixed window would otherwise never accumulate
    enough to earn a delay.
    """
    if not username:
        return BackoffState(is_delayed=False)

    count_key, until_key = _keys(tenant_id, username)

    failures = cache.get(count_key, 0) + 1
    cache.set(count_key, failures, _decay_seconds())

    delay = delay_for(failures)
    if delay <= 0:
        return BackoffState(is_delayed=False, failures=failures)

    cache.set(until_key, time.time() + delay, delay)
    return BackoffState(is_delayed=True, retry_after_seconds=delay, failures=failures)


def clear(tenant_id, username: str) -> None:
    """Forget past failures after a successful sign-in."""
    if not username:
        return
    cache.delete_many(list(_keys(tenant_id, username)))
