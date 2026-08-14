"""
Working out who sent a request.

This exists because ``X-Forwarded-For`` is attacker-controlled in its early
entries, and the M-Pesa callback allowlist decides whether real money is
credited on the strength of an address. Getting the wrong entry out of that
header would make the allowlist trivially bypassable: a caller simply sets
``X-Forwarded-For: 196.201.214.200`` and looks like Safaricom.

**The header is built left to right, and each proxy appends what it saw.** With
one trusted proxy in front of Django - which is the shape this deploys in, a
single nginx or Caddy terminating TLS - the header looks like::

    X-Forwarded-For: <whatever the client sent>, <address our proxy saw>

The *last* entry is the one our own proxy appended, so it is the only entry we
have any reason to believe. Everything to the left of it is unverified: it may
be a genuine chain of upstream proxies, or it may be something a caller made up.

Taking the first entry - the common default, and what this code used to do - is
therefore exactly backwards for any security decision. It is the entry most
under an attacker's control.

The number of trusted hops is configuration rather than a guess, and production
refuses to start without it: see ``pos.E004``.
"""

from __future__ import annotations

from django.conf import settings


def client_ip(request, *, trusted_hops: int | None = None) -> str | None:
    """The client address, counting back past our own trusted proxies.

    ``trusted_hops`` is how many proxies of ours sit in front of Django. One
    means the last entry in the header; two means the second from last, and so
    on. Zero means ignore the header entirely and use the socket address, which
    is right when nothing is in front.
    """
    hops = (
        trusted_hops
        if trusted_hops is not None
        else getattr(settings, "TRUSTED_PROXY_HOPS", 0)
    )

    remote_addr = request.META.get("REMOTE_ADDR") or None
    if hops <= 0:
        return remote_addr

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded:
        # No header at all. Either nothing is in front after all, or something
        # is misconfigured; the socket address is the only thing left and it is
        # at least real.
        return remote_addr

    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
    if not entries:
        return remote_addr

    # Count back from the right. With one trusted hop that is entries[-1].
    index = len(entries) - hops
    if index < 0:
        # Fewer entries than we expect hops. Someone has stripped the header, or
        # the hop count is wrong. Refusing to guess: the leftmost entry is the
        # most attacker-controlled one, so returning it would be the worst
        # available answer.
        return None
    return entries[index]


def is_ip_allowed(request, allowed: list[str], *, trusted_hops: int | None = None) -> bool:
    """Whether a request came from an address on a list.

    Fails closed on an empty list. "Not configured" and "allow everything" must
    not be the same thing, or forgetting to fill it in looks identical to having
    filled it in correctly.
    """
    if not allowed:
        return False

    address = client_ip(request, trusted_hops=trusted_hops)
    if address is None:
        return False
    return address in {entry.strip() for entry in allowed}
