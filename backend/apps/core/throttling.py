"""Rate limiting that counts the right client.

DRF's own throttles decide who is asking with ``BaseThrottle.get_ident``, which
reads ``X-Forwarded-For`` through its own ``NUM_PROXIES`` setting - a setting
this project never configured. With it unset, DRF takes the branch that returns
the *entire* header, whitespace stripped, as the throttle identity::

    return ''.join(xff.split()) if xff else remote_addr

So every distinct header value was its own bucket. Sending a different
``X-Forwarded-For`` on each attempt gave an attacker a fresh allowance every
time, and the sign-in rate limit did nothing at all. Measured before this
existed: twelve sign-in attempts against a five-per-minute limit produced seven
429s from a fixed address and **none** when the header varied.

This is the same mistake ``apps.core.net`` exists to prevent, arriving by a
different route. That module was applied to the M-Pesa allowlist and the audit
trail, but DRF resolves the client address independently and was never brought
into line.

Setting ``NUM_PROXIES`` would also have closed it, and is rejected on purpose:
it is a second proxy-depth setting that can drift out of step with
``TRUSTED_PROXY_HOPS``, and two answers to "how many proxies are in front of
us" is how this class of bug happens twice.
"""

from __future__ import annotations

from rest_framework.throttling import ScopedRateThrottle

from apps.core.net import client_ip

# Requests whose address cannot be resolved share one bucket rather than each
# getting their own. ``client_ip`` returns None when the header carries fewer
# entries than there are trusted hops, because it refuses to guess.
#
# To be accurate about what this does and does not buy: ``ScopedRateThrottle``
# formats its cache key unconditionally, so a None ident would become the
# literal string "None" and still be counted. Returning None here would *not*
# fail open. The sentinel is for legibility and to stay correct if the base
# class ever changes - not a fix for a live hole. The mutation that genuinely
# breaks this property is giving each unresolved request a unique value, which
# is the plausible mistake somebody makes while "improving" this line, and the
# test is written against that.
UNRESOLVED = "unresolved-address"

# PIN sign-in is counted per till, not per shop. Every till in a duka sits
# behind one NAT address, so an address-only limit would let one misbehaving
# device exhaust the allowance for every till in the building - and refusing
# sign-in during trade is its own kind of outage. A device token is already
# required for PIN sign-in, so keying on it as well costs nothing.
DEVICE_SCOPED = frozenset({"pin-login"})


class ClientAddressScopedThrottle(ScopedRateThrottle):
    """``ScopedRateThrottle`` that agrees with the rest of the system.

    ``self.scope`` is set by ``allow_request`` from the view before
    ``get_cache_key`` calls ``get_ident``, so keying can depend on it.
    """

    def get_ident(self, request) -> str:
        address = client_ip(request) or UNRESOLVED

        if self.scope not in DEVICE_SCOPED:
            return address

        return f"{address}|{self._device_fingerprint(request)}"

    @staticmethod
    def _device_fingerprint(request) -> str:
        """The device a PIN attempt claims to come from.

        Hashed rather than raw, so a token never reaches a cache key, and read
        defensively: a throttle runs before the serializer, so the body may be
        malformed, absent, or not a mapping at all. Anything unreadable counts
        as one shared bucket rather than as its own.
        """
        from apps.accounts.models import hash_device_token

        try:
            data = request.data
        except Exception:
            # Unparseable body. Deliberately broad: a throttle that raises
            # turns a malformed request into a 500 and, worse, skips the limit.
            return UNRESOLVED

        if not isinstance(data, dict):
            return UNRESOLVED

        token = data.get("device_token")
        if not token or not isinstance(token, str):
            return UNRESOLVED

        return hash_device_token(token)
