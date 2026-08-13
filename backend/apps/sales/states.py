"""
The sale state machine.

```
                     tender cash
        ┌────────┐ ───────────────▶ ┌──────┐ ◀── callback ok ── ┌──────────────────┐
        │  OPEN  │                  │ PAID │                    │ AWAITING_PAYMENT │
        └────────┘ ◀── fail/timeout ┴──────┴─────────────────── └──────────────────┘
          │    │         (from AWAITING_PAYMENT)                   │
          │    └──── STK push ─────────────────────────────────────┘
          │                              │      │
          │ abandon                      │      │ full refund
          ▼                              │      ▼
        ┌──────┐                         │   ┌──────────┐
        │ VOID │ ◀── abandon, if unpaid ─┘   │ REFUNDED │
        └──────┘                             └──────────┘
                       partial refund   ▲          ▲
                              │         │          │ remaining refunded
                              ▼         │          │
                    ┌──────────────────────┐───────┘
                    │  PARTIALLY_REFUNDED  │
                    └──────────────────────┘
                         further partials
```

Two properties are worth stating plainly, because both are load-bearing.

**A paid sale cannot be voided.** ``VOID`` is reachable only from ``OPEN`` and
``AWAITING_PAYMENT``, and only while no payment has succeeded. Once money has
moved the correction is a refund, which records an amount, an actor and a
reason. A void that erased a settled sale is exactly how a dishonest cashier
would remove a sale after pocketing the cash, and no convenience is worth
leaving that path open.

**State is derived, not set.** ``PAID``, ``PARTIALLY_REFUNDED`` and ``REFUNDED``
are computed from the payment and refund ledgers and cached on the row, the same
way a stock quantity caches its movements. :func:`recompute_state` is the only
writer, so the cached value cannot drift from the ledgers that justify it.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models


class SaleState(models.TextChoices):
    """Where a sale has got to."""

    OPEN = "OPEN", "Open"
    AWAITING_PAYMENT = "AWAITING_PAYMENT", "Awaiting payment"
    PAID = "PAID", "Paid"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
    REFUNDED = "REFUNDED", "Refunded"
    VOID = "VOID", "Void"


#: States in which a sale is finished and its lines are frozen.
TERMINAL_STATES = frozenset({SaleState.REFUNDED, SaleState.VOID})

#: States in which the lines may still be edited. Anything else is immutable,
#: which is what makes offline sync tractable: no update path, so no merge.
EDITABLE_STATES = frozenset({SaleState.OPEN})

#: States in which money has been taken and not fully returned.
SETTLED_STATES = frozenset(
    {SaleState.PAID, SaleState.PARTIALLY_REFUNDED, SaleState.REFUNDED}
)

#: The only state in which an M-Pesa callback may credit a payment.
#:
#: Exactly one state, deliberately. A callback for a sale in *any* other state -
#: void, already paid, or timed back out to OPEN - is neither credited nor
#: dropped: it is recorded as suspect and surfaced for manual reconciliation.
#:
#: The awkward case this creates is worth naming rather than hiding. A push that
#: times out sends the sale back to OPEN, and a customer who enters their PIN
#: late then produces a genuine payment that will *not* be credited
#: automatically. That is the conservative direction: money the shop holds but
#: has not applied is a discrepancy someone resolves, whereas auto-crediting a
#: sale that has since been re-rung or paid in cash would double-charge a
#: customer who did nothing wrong.
CREDITABLE_STATES = frozenset({SaleState.AWAITING_PAYMENT})


#: Every legal move. Anything absent is refused by :func:`assert_can_transition`.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    SaleState.OPEN: frozenset(
        {SaleState.AWAITING_PAYMENT, SaleState.PAID, SaleState.VOID}
    ),
    SaleState.AWAITING_PAYMENT: frozenset(
        {SaleState.OPEN, SaleState.PAID, SaleState.VOID}
    ),
    SaleState.PAID: frozenset({SaleState.PARTIALLY_REFUNDED, SaleState.REFUNDED}),
    SaleState.PARTIALLY_REFUNDED: frozenset(
        {SaleState.PARTIALLY_REFUNDED, SaleState.REFUNDED}
    ),
    # Terminal.
    SaleState.REFUNDED: frozenset(),
    SaleState.VOID: frozenset(),
}


class IllegalTransition(Exception):
    """A move the state machine does not allow.

    Raised rather than logged, because every one of these is either a bug or an
    attempt to do something the machine exists to prevent - most importantly,
    erasing a sale that has already taken money.
    """

    def __init__(self, current: str, requested: str, reason: str = ""):
        self.current = current
        self.requested = requested
        detail = f"A sale cannot move from {current} to {requested}."
        if reason:
            detail = f"{detail} {reason}"
        super().__init__(detail)
        self.detail = detail


def can_transition(current: str, requested: str) -> bool:
    """Whether this move is legal, ignoring any ledger conditions."""
    return requested in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_can_transition(current: str, requested: str, reason: str = "") -> None:
    """Raise unless the move is legal."""
    if not can_transition(current, requested):
        raise IllegalTransition(current, requested, reason)


@dataclass(frozen=True)
class LedgerPosition:
    """What the payment and refund ledgers say about one sale.

    Computed from the ledgers rather than read off the sale, so it is the thing
    the cached state is checked against rather than a restatement of it.
    """

    total_cents: int
    paid_cents: int
    refunded_cents: int

    @property
    def outstanding_cents(self) -> int:
        """Still owed by the customer. Never negative; see ``overpaid_cents``."""
        return max(0, self.total_cents - self.paid_cents)

    @property
    def overpaid_cents(self) -> int:
        """Taken beyond the total.

        Not an impossibility to be asserted away. Two successful STK pushes
        against one sale genuinely charge a customer twice, and the money is
        real; recording it and blocking on it is the only honest response.
        """
        return max(0, self.paid_cents - self.total_cents)

    @property
    def is_settled(self) -> bool:
        return self.paid_cents >= self.total_cents and self.total_cents > 0

    @property
    def is_fully_refunded(self) -> bool:
        return self.total_cents > 0 and self.refunded_cents >= self.total_cents

    @property
    def refundable_cents(self) -> int:
        """What could still be refunded, capped at what was actually taken."""
        return max(0, min(self.paid_cents, self.total_cents) - self.refunded_cents)


def derive_state(current: str, position: LedgerPosition) -> str:
    """Work out the state the ledgers imply.

    **This is reconciliation, not a transition**, and the distinction is worth
    being precise about because conflating the two is a bug I wrote once already.

    :data:`ALLOWED_TRANSITIONS` governs moves a *person* initiates - tendering,
    voiding, refunding. It is what stops a paid sale being voided.

    This function instead answers "given these ledgers, what state should the
    cached column hold?" A stale cache catching up may legitimately cross more
    than one edge at once: a sale still marked ``OPEN`` whose ledgers show it was
    paid and partly refunded resolves straight to ``PARTIALLY_REFUNDED``, because
    both of those things demonstrably happened. Forcing that through the
    transition table would refuse to record history that is already in the
    ledgers.

    The two rules that do hold here:

    * ``VOID`` is sticky. A late payment against a voided sale cannot resurrect
      it - that money is a discrepancy for a person to resolve, not a state
      change. This is what the M-Pesa callback guard relies on.
    * The result always matches the ledgers, so the cached state can never
      claim something the payment and refund rows do not support.

    Ledgers that could not legitimately arise - a refund against a sale that was
    never paid - are prevented at the service layer, where a refund checks the
    sale is settled before writing a row.
    """
    if current == SaleState.VOID:
        return SaleState.VOID

    if position.is_fully_refunded:
        return SaleState.REFUNDED
    if position.refunded_cents > 0:
        return SaleState.PARTIALLY_REFUNDED
    if position.is_settled:
        return SaleState.PAID

    # Nothing settled yet: stay where we are if that is still coherent, which
    # keeps a sale waiting on an STK push from falling back to OPEN.
    if current in (SaleState.OPEN, SaleState.AWAITING_PAYMENT):
        return current
    return SaleState.OPEN
