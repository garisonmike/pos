"""
The sale state machine.

Every legal move is asserted, and so is every illegal one — the illegal set is
the point of having a machine at all. The single most important assertion in
this file is that a paid sale cannot be voided, because that is the transition a
dishonest cashier would want after pocketing the cash.
"""

from __future__ import annotations

import itertools

import pytest

from apps.sales.states import (
    ALLOWED_TRANSITIONS,
    CREDITABLE_STATES,
    EDITABLE_STATES,
    TERMINAL_STATES,
    IllegalTransition,
    LedgerPosition,
    SaleState,
    assert_can_transition,
    can_transition,
    derive_state,
)

ALL_STATES = list(SaleState.values)


class TestLegalTransitions:
    @pytest.mark.parametrize(
        ("current", "requested"),
        [
            (SaleState.OPEN, SaleState.AWAITING_PAYMENT),
            (SaleState.OPEN, SaleState.PAID),
            (SaleState.OPEN, SaleState.VOID),
            (SaleState.AWAITING_PAYMENT, SaleState.PAID),
            (SaleState.AWAITING_PAYMENT, SaleState.VOID),
            (SaleState.AWAITING_PAYMENT, SaleState.OPEN),
            (SaleState.PAID, SaleState.PARTIALLY_REFUNDED),
            (SaleState.PAID, SaleState.REFUNDED),
            (SaleState.PARTIALLY_REFUNDED, SaleState.PARTIALLY_REFUNDED),
            (SaleState.PARTIALLY_REFUNDED, SaleState.REFUNDED),
        ],
    )
    def test_allowed(self, current, requested):
        assert can_transition(current, requested) is True

    def test_a_failed_push_returns_the_sale_to_the_cart(self):
        """So the cashier can retry, or switch to cash, without re-ringing."""
        assert can_transition(SaleState.AWAITING_PAYMENT, SaleState.OPEN) is True


class TestTheTransitionThatMustNeverBeAllowed:
    def test_a_paid_sale_cannot_be_voided(self):
        """The one that matters most.

        Once money has moved, the correction is a refund - which records an
        amount, an actor and a reason. A void that erased a settled sale is
        precisely how a sale would be removed after the cash was pocketed.
        """
        assert can_transition(SaleState.PAID, SaleState.VOID) is False

        with pytest.raises(IllegalTransition):
            assert_can_transition(SaleState.PAID, SaleState.VOID)

    def test_a_refunded_sale_cannot_be_voided(self):
        assert can_transition(SaleState.REFUNDED, SaleState.VOID) is False

    def test_a_partly_refunded_sale_cannot_be_voided(self):
        assert can_transition(SaleState.PARTIALLY_REFUNDED, SaleState.VOID) is False

    def test_no_settled_state_can_reach_void(self):
        """Stated as a sweep, so a new settled state cannot quietly gain the path."""
        for state in (SaleState.PAID, SaleState.PARTIALLY_REFUNDED, SaleState.REFUNDED):
            assert SaleState.VOID not in ALLOWED_TRANSITIONS[state]


class TestTerminalStates:
    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
    def test_nothing_leaves_a_terminal_state(self, state):
        assert ALLOWED_TRANSITIONS[state] == frozenset()

    @pytest.mark.parametrize(
        ("state", "target"),
        list(itertools.product(sorted(TERMINAL_STATES), ALL_STATES)),
    )
    def test_every_move_out_of_a_terminal_state_is_refused(self, state, target):
        assert can_transition(state, target) is False

    def test_a_void_sale_stays_void_however_the_ledgers_look(self):
        """A late payment cannot resurrect a voided sale.

        This is the race the callback guard exists for: the money is real, but
        it belongs to a discrepancy a person resolves, not to a state change.
        """
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=0)
        assert derive_state(SaleState.VOID, position) == SaleState.VOID


class TestIllegalTransitionsAreExhaustive:
    def test_every_pair_not_declared_legal_is_refused(self):
        """Sweeps all 36 pairs, so nothing is legal by omission."""
        for current, requested in itertools.product(ALL_STATES, ALL_STATES):
            expected = requested in ALLOWED_TRANSITIONS.get(current, frozenset())
            assert can_transition(current, requested) is expected

    def test_the_error_names_both_states(self):
        """A refusal has to be diagnosable from the message alone."""
        with pytest.raises(IllegalTransition) as raised:
            assert_can_transition(SaleState.REFUNDED, SaleState.OPEN)

        assert "REFUNDED" in str(raised.value)
        assert "OPEN" in str(raised.value)


class TestEditability:
    def test_only_an_open_sale_may_be_edited(self):
        assert EDITABLE_STATES == frozenset({SaleState.OPEN})

    @pytest.mark.parametrize(
        "state",
        [s for s in ALL_STATES if s != SaleState.OPEN],
    )
    def test_everything_else_is_frozen(self, state):
        """No update path is what makes offline sync tractable: no merge."""
        assert state not in EDITABLE_STATES


class TestCallbackCreditability:
    def test_only_a_sale_awaiting_payment_may_be_credited(self):
        assert CREDITABLE_STATES == frozenset({SaleState.AWAITING_PAYMENT})

    @pytest.mark.parametrize(
        "state",
        [s for s in ALL_STATES if s != SaleState.AWAITING_PAYMENT],
    )
    def test_every_other_state_refuses_a_callback(self, state):
        """Including OPEN.

        A push that timed out sends the sale back to OPEN, so a late success is
        genuine money that will not be credited automatically. That is the
        conservative direction: auto-crediting a sale since re-rung or paid in
        cash would double-charge a customer who did nothing wrong.
        """
        assert state not in CREDITABLE_STATES


class TestLedgerPosition:
    def test_outstanding_on_an_unpaid_sale(self):
        position = LedgerPosition(total_cents=18000, paid_cents=0, refunded_cents=0)
        assert position.outstanding_cents == 18000
        assert position.is_settled is False

    def test_a_part_payment_leaves_a_balance(self):
        """Split payments: part cash now, the rest by M-Pesa."""
        position = LedgerPosition(total_cents=18000, paid_cents=10000, refunded_cents=0)
        assert position.outstanding_cents == 8000
        assert position.is_settled is False

    def test_paying_the_balance_settles_it(self):
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=0)
        assert position.outstanding_cents == 0
        assert position.is_settled is True

    def test_overpayment_is_reported_rather_than_clamped_away(self):
        """Two successful pushes genuinely charge a customer twice.

        The money is real, so the excess is surfaced instead of being asserted
        out of existence.
        """
        position = LedgerPosition(total_cents=18000, paid_cents=36000, refunded_cents=0)
        assert position.overpaid_cents == 18000
        assert position.outstanding_cents == 0

    def test_refundable_never_exceeds_what_was_taken(self):
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=5000)
        assert position.refundable_cents == 13000

    def test_nothing_is_refundable_once_fully_refunded(self):
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=18000)
        assert position.refundable_cents == 0
        assert position.is_fully_refunded is True


class TestDerivedState:
    def test_an_unpaid_open_sale_stays_open(self):
        position = LedgerPosition(total_cents=18000, paid_cents=0, refunded_cents=0)
        assert derive_state(SaleState.OPEN, position) == SaleState.OPEN

    def test_a_sale_awaiting_payment_stays_there_until_money_lands(self):
        position = LedgerPosition(total_cents=18000, paid_cents=0, refunded_cents=0)
        assert derive_state(SaleState.AWAITING_PAYMENT, position) == SaleState.AWAITING_PAYMENT

    def test_a_part_payment_does_not_settle_a_sale(self):
        position = LedgerPosition(total_cents=18000, paid_cents=10000, refunded_cents=0)
        assert derive_state(SaleState.AWAITING_PAYMENT, position) == SaleState.AWAITING_PAYMENT

    def test_full_payment_settles_it(self):
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=0)
        assert derive_state(SaleState.AWAITING_PAYMENT, position) == SaleState.PAID

    def test_a_partial_refund(self):
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=5000)
        assert derive_state(SaleState.PAID, position) == SaleState.PARTIALLY_REFUNDED

    def test_refunding_the_remainder(self):
        position = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=18000)
        assert derive_state(SaleState.PARTIALLY_REFUNDED, position) == SaleState.REFUNDED

    def test_overpayment_still_reads_as_paid(self):
        """The excess is flagged separately; the sale itself is settled."""
        position = LedgerPosition(total_cents=18000, paid_cents=36000, refunded_cents=0)
        assert derive_state(SaleState.AWAITING_PAYMENT, position) == SaleState.PAID

    def test_reconciliation_may_cross_more_than_one_edge_at_once(self):
        """Deriving a state is not the same as transitioning to one.

        A cached state that has fallen behind its ledgers must be able to catch
        up in one step. A sale still marked OPEN whose ledgers show it was paid
        and partly refunded resolves straight to PARTIALLY_REFUNDED, because both
        of those things demonstrably happened - forcing it through the transition
        table would refuse to record history already written in the ledgers.

        The transition table governs what a *person* may do. This governs what
        the ledgers *say*.
        """
        stale = LedgerPosition(total_cents=18000, paid_cents=18000, refunded_cents=5000)

        assert derive_state(SaleState.OPEN, stale) == SaleState.PARTIALLY_REFUNDED
        assert can_transition(SaleState.OPEN, SaleState.PARTIALLY_REFUNDED) is False

    def test_the_derived_state_never_contradicts_the_ledgers(self):
        """The invariant that does hold across every state and position."""
        positions = [
            LedgerPosition(18000, 0, 0),
            LedgerPosition(18000, 10000, 0),
            LedgerPosition(18000, 18000, 0),
            LedgerPosition(18000, 18000, 5000),
            LedgerPosition(18000, 18000, 18000),
            LedgerPosition(18000, 36000, 0),
        ]
        for current in ALL_STATES:
            for position in positions:
                derived = derive_state(current, position)

                if current == SaleState.VOID:
                    assert derived == SaleState.VOID
                    continue
                if position.is_fully_refunded:
                    assert derived == SaleState.REFUNDED
                elif position.refunded_cents > 0:
                    assert derived == SaleState.PARTIALLY_REFUNDED
                elif position.is_settled:
                    assert derived == SaleState.PAID
                else:
                    assert derived in (SaleState.OPEN, SaleState.AWAITING_PAYMENT)

    def test_void_is_sticky_against_every_ledger(self):
        """What the M-Pesa callback guard relies on."""
        for position in (
            LedgerPosition(18000, 18000, 0),
            LedgerPosition(18000, 36000, 0),
            LedgerPosition(18000, 18000, 18000),
        ):
            assert derive_state(SaleState.VOID, position) == SaleState.VOID
