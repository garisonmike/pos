"""
Lockout for till PIN sign-in.

A four-digit PIN is only an acceptable credential because it is half of one -
the other half is possession of a registered device. But a device that has been
stolen, or simply left on a counter, gives an attacker unlimited attempts at a
space of ten thousand PINs. At a few requests a second that falls in minutes.

A request-rate limit alone does not fix this. Rate limiting caps how fast
attempts arrive; it does not cap how many arrive in total, so a patient attacker
just works within the limit. What is needed is a *lockout*: after a handful of
consecutive failures the device stops being accepted at all for a while,
regardless of how slowly the attempts came in.

Two counters, because there are two distinct attacks:

``device``
    Many PINs tried against one till. This is the stolen-tablet case and the
    main thing being defended against. Locking the device stops it dead.

``user``
    One cashier's PIN tried across several tills, which sidesteps a per-device
    counter entirely. Rarer, but cheap to close.

Both live in the shared cache rather than the database. Failed attempts are
high-frequency, worthless after they expire, and must be visible to every API
worker at once - a per-process counter would hand an attacker one full set of
attempts per worker. They are recorded in the audit trail as well, which is the
durable record; the cache only decides whether to refuse the next attempt.

Counters are cleared on a successful sign-in, so a cashier who fumbles their PIN
twice and then gets it right starts clean rather than carrying failures around
for the rest of their shift.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

_DEVICE_KEY = "pinfail:device:{tenant_id}:{device_id}"
_USER_KEY = "pinfail:user:{tenant_id}:{username}"


@dataclass(frozen=True)
class LockoutState:
    """Whether sign-in is currently refused, and for how long."""

    is_locked: bool
    retry_after_seconds: int = 0
    attempts: int = 0

    @property
    def retry_after_minutes(self) -> int:
        """Rounded up, because telling a cashier to wait "0 minutes" is useless."""
        return max(1, -(-self.retry_after_seconds // 60))


def _max_attempts() -> int:
    return getattr(settings, "PIN_LOCKOUT_MAX_ATTEMPTS", 5)


def _lockout_seconds() -> int:
    return getattr(settings, "PIN_LOCKOUT_SECONDS", 900)


def _keys(tenant_id, device_id, username: str) -> list[str]:
    """The counter keys relevant to one sign-in attempt."""
    keys = [_DEVICE_KEY.format(tenant_id=tenant_id, device_id=device_id)]
    if username:
        keys.append(_USER_KEY.format(tenant_id=tenant_id, username=username.lower()))
    return keys


def check(tenant_id, device_id, username: str = "") -> LockoutState:
    """Report whether this device or user is currently locked out.

    Called before the PIN is checked, so a locked device is refused without the
    attempt ever being evaluated. That matters: evaluating it would let an
    attacker distinguish "locked" from "wrong PIN" by response timing.
    """
    limit = _max_attempts()
    worst = LockoutState(is_locked=False)

    for key in _keys(tenant_id, device_id, username):
        attempts = cache.get(key, 0)
        if attempts >= limit:
            ttl = _ttl(key)
            if ttl > worst.retry_after_seconds:
                worst = LockoutState(
                    is_locked=True, retry_after_seconds=ttl, attempts=attempts
                )
        elif not worst.is_locked and attempts > worst.attempts:
            worst = LockoutState(is_locked=False, attempts=attempts)

    return worst


def record_failure(tenant_id, device_id, username: str = "") -> LockoutState:
    """Count a failed attempt and report the resulting state.

    The window is refreshed on every failure rather than counting within a fixed
    window from the first one. An attacker who paces attempts to straddle a
    fixed window would otherwise never accumulate enough to trip the limit.
    """
    limit = _max_attempts()
    timeout = _lockout_seconds()
    highest = 0

    for key in _keys(tenant_id, device_id, username):
        # add() only writes when the key is absent, so the first failure sets
        # the value and its expiry together; incr() then bumps it without
        # touching the expiry, which is why the set below rewrites it.
        attempts = cache.get(key, 0) + 1
        cache.set(key, attempts, timeout)
        highest = max(highest, attempts)

    if highest >= limit:
        return LockoutState(
            is_locked=True, retry_after_seconds=timeout, attempts=highest
        )
    return LockoutState(is_locked=False, attempts=highest)


def clear(tenant_id, device_id, username: str = "") -> None:
    """Forget past failures after a successful sign-in."""
    cache.delete_many(_keys(tenant_id, device_id, username))


def attempts_remaining(state: LockoutState) -> int:
    """How many tries are left before a lockout, for the message shown at the till."""
    return max(0, _max_attempts() - state.attempts)


def _ttl(key: str) -> int:
    """Seconds until a counter expires.

    Backends disagree here: Redis reports the real remaining time, local memory
    returns ``None``. Falling back to the full lockout period is the safe
    direction to be wrong in - it may tell a cashier to wait slightly longer
    than strictly necessary, rather than inviting them to retry immediately.
    """
    try:
        ttl = cache.ttl(key)  # type: ignore[attr-defined]
    except (AttributeError, NotImplementedError):
        return _lockout_seconds()
    if ttl is None:
        return _lockout_seconds()
    return int(ttl)
