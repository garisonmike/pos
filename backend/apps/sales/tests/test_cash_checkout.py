"""
Cash checkout, end to end, and the authority a discount needs.

The authorisation matrix is the substance here. A discount is the simplest way
to move money out of a shop - ring something up, discount it to nothing, keep
the difference - so the tests that matter are the ones where somebody tries to
go around the gate rather than through it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from apps.accounts.constants import UserRole
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context
from apps.inventory.models import StockItem
from apps.sales.models import Sale
from apps.sales.states import ALLOWED_TRANSITIONS, SaleState

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/sales/checkout/cash/"


@pytest.fixture
def manager_with_pin(tenant_a):
    """A manager who can approve a discount at the till."""
    from apps.accounts.models import User

    with transaction.atomic(), tenant_context(tenant_a.id):
        user = User(
            tenant=tenant_a,
            username="peter",
            full_name="Peter Omondi",
            role=UserRole.MANAGER,
        )
        user.set_password("peter-pass-8823")
        user.set_pin("4471")
        user.save()
        return user


@pytest.fixture
def second_cashier(tenant_a):
    """Another cashier, for the does-a-cashier-count-as-authority test."""
    from apps.accounts.models import User

    with transaction.atomic(), tenant_context(tenant_a.id):
        user = User(
            tenant=tenant_a, username="joyce", full_name="Joyce A", role=UserRole.CASHIER
        )
        user.set_password("joyce-pass-1177")
        user.set_pin("9182")
        user.save()
        return user


@pytest.fixture
def manager_in_other_tenant(tenant_b):
    """A manager of a different business entirely."""
    from apps.accounts.models import User

    with transaction.atomic(), tenant_context(tenant_b.id):
        user = User(
            tenant=tenant_b, username="theirs", full_name="Their Manager", role=UserRole.MANAGER
        )
        user.set_password("theirs-pass-3391")
        user.set_pin("5566")
        user.save()
        return user


def body(item, **overrides):
    payload = {
        "lines": [{"item_id": str(item.id), "quantity": "1"}],
        "tendered_cents": 20000,
    }
    payload.update(overrides)
    return payload


class TestTheFullPath:
    def test_cart_to_paid_with_an_authorized_discount(
        self, client_cashier_a, item_a, stock_a, manager_with_pin, tenant_a
    ):
        """Cart, manager approval, cash, PAID, receipt number allocated."""
        response = client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                lines=[{"item_id": str(item_a.id), "quantity": "1", "discount_cents": 2000}],
                discount_authorization={
                    "username": "peter",
                    "pin": "4471",
                    "reason": "Damaged packaging",
                },
                tendered_cents=20000,
            ),
            format="json",
        )
        assert response.status_code == 201, response.content

        sale = response.json()
        assert sale["state"] == SaleState.PAID
        assert sale["discount_cents"] == 2000
        assert sale["total_cents"] == 16000
        assert sale["receipt_number"] == 1
        assert sale["receipt_code"].startswith("INV-")
        assert sale["discount_authorized_label"] == "peter"
        assert sale["discount_authorized_via"] == "PIN"
        assert sale["discount_authorization_reason"] == "Damaged packaging"

    def test_the_final_state_is_one_the_machine_allows(
        self, client_cashier_a, item_a, stock_a
    ):
        """The endpoint must not reach a state the transition table forbids."""
        response = client_cashier_a.post(CHECKOUT, body(item_a), format="json")

        assert response.json()["state"] == SaleState.PAID
        assert SaleState.PAID in ALLOWED_TRANSITIONS[SaleState.OPEN]

    def test_change_is_returned(self, client_cashier_a, item_a, stock_a):
        response = client_cashier_a.post(
            CHECKOUT, body(item_a, tendered_cents=20000), format="json"
        )
        assert response.json()["change_cents"] == 2000

    def test_stock_moves_on_settlement(self, client_cashier_a, item_a, stock_a, tenant_a):
        client_cashier_a.post(CHECKOUT, body(item_a), format="json")

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("39.000")

    def test_receipt_numbers_are_sequential(self, client_cashier_a, item_a, stock_a):
        first = client_cashier_a.post(CHECKOUT, body(item_a), format="json").json()
        second = client_cashier_a.post(CHECKOUT, body(item_a), format="json").json()

        assert (first["receipt_number"], second["receipt_number"]) == (1, 2)

    def test_a_manager_authorizes_from_their_own_session(
        self, client_manager_a, item_a, stock_a
    ):
        """No PIN re-entry: their session is the authority.

        The same authority that already lets them adjust stock and refund a
        sale. Asking them to prove it again at the till would be a ritual, and
        rituals get worked around.
        """
        response = client_manager_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=2000,
                discount_authorization={"reason": "Regular customer"},
            ),
            format="json",
        )
        assert response.status_code == 201, response.content

        sale = response.json()
        assert sale["discount_authorized_via"] == "SESSION"
        assert sale["discount_authorized_label"] == "mngr"
        assert sale["total_cents"] == 16000


class TestDiscountAuthorizationMatrix:
    """Seven cases. Five of them are somebody trying to go around the gate."""

    def test_manager_actor_with_a_reason_is_applied(
        self, client_manager_a, item_a, stock_a
    ):
        response = client_manager_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={"reason": "Goodwill"},
            ),
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["discount_authorized_via"] == "SESSION"

    def test_manager_actor_without_a_reason_is_refused(
        self, client_manager_a, item_a, stock_a
    ):
        """Authority is not the only requirement; the record needs a why."""
        response = client_manager_a.post(
            CHECKOUT, body(item_a, cart_discount_cents=1000), format="json"
        )
        assert response.status_code == 403
        assert response.json()["code"] == "discount_reason_required"

    def test_cashier_actor_with_no_authorization_block_is_refused(
        self, client_cashier_a, item_a, stock_a
    ):
        response = client_cashier_a.post(
            CHECKOUT, body(item_a, cart_discount_cents=1000), format="json"
        )
        assert response.status_code == 403
        assert response.json()["code"] == "discount_authorization_required"

    def test_cashier_actor_naming_a_manager_with_no_credential_is_refused(
        self, client_cashier_a, item_a, stock_a, manager_with_pin
    ):
        """An id alone is not a gate.

        If the cashier can type the id, the cashier can type any id - so this
        must fail exactly as if no manager had been named at all.
        """
        response = client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={"username": "peter", "reason": "Because"},
            ),
            format="json",
        )
        assert response.status_code == 403
        assert response.json()["code"] == "discount_authorization_required"

    def test_cashier_actor_with_a_wrong_pin_is_refused(
        self, client_cashier_a, item_a, stock_a, manager_with_pin
    ):
        response = client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "peter",
                    "pin": "0000",
                    "reason": "Because",
                },
            ),
            format="json",
        )
        assert response.status_code == 403

    def test_another_cashiers_correct_pin_is_refused_on_role(
        self, client_cashier_a, item_a, stock_a, second_cashier
    ):
        """A correct credential is not the same as authority."""
        response = client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "joyce",
                    "pin": "9182",
                    "reason": "Because",
                },
            ),
            format="json",
        )
        assert response.status_code == 403

    def test_a_manager_from_another_business_is_not_found(
        self, client_cashier_a, item_a, stock_a, manager_in_other_tenant
    ):
        """Isolation does this for free: the user table is already scoped."""
        response = client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "theirs",
                    "pin": "5566",
                    "reason": "Because",
                },
            ),
            format="json",
        )
        assert response.status_code == 403


class TestNothingIsWrittenWhenAuthorizationFails:
    """A refusal must leave no sale, not merely return an error code."""

    @pytest.mark.parametrize(
        "authorization",
        [
            None,
            {"username": "peter", "reason": "Because"},
            {"username": "peter", "pin": "0000", "reason": "Because"},
        ],
    )
    def test_no_sale_is_created(
        self, client_cashier_a, item_a, stock_a, manager_with_pin, tenant_a, authorization
    ):
        payload = body(item_a, cart_discount_cents=1000)
        if authorization is not None:
            payload["discount_authorization"] = authorization

        client_cashier_a.post(CHECKOUT, payload, format="json")

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert Sale.objects.count() == 0

    def test_stock_is_untouched(
        self, client_cashier_a, item_a, stock_a, manager_with_pin, tenant_a
    ):
        client_cashier_a.post(
            CHECKOUT, body(item_a, cart_discount_cents=1000), format="json"
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            stock_a.refresh_from_db()
        assert stock_a.quantity == Decimal("40.000")


class TestFailedAuthorizationIsAudited:
    """check_pin is a pure predicate and audits nothing.

    The sign-in view writes its own failures for the same reason. Without an
    entry here, somebody standing at a till working through a manager's four
    digits would leave no trace - which is exactly the trace a shop owner needs.
    """

    def test_a_wrong_pin_is_recorded(
        self, client_cashier_a, item_a, stock_a, manager_with_pin, tenant_a
    ):
        client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "peter",
                    "pin": "0000",
                    "reason": "Because",
                },
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).first()

        assert entry is not None
        assert entry.entity_id == "peter"
        assert entry.reason == "bad_credential"

    def test_the_entry_names_the_cashier_but_not_the_manager(
        self, client_cashier_a, cashier_a, item_a, stock_a, manager_with_pin, tenant_a
    ):
        """Filed against the username string, like a failed sign-in.

        Nobody has proved they are that manager, so attaching them by foreign
        key would put someone else's guessing into their history. The acting
        cashier *is* recorded: unlike a sign-in this happened inside an
        authenticated session, so who held the till is known and worth knowing.
        """
        client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "peter",
                    "pin": "0000",
                    "reason": "Because",
                },
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).first()

        assert entry.actor_id == cashier_a.id
        assert entry.after["acting_cashier"] == "mary"
        assert entry.after["attempted_authorizer"] == "peter"

    def test_the_pin_is_never_in_the_entry(
        self, client_cashier_a, item_a, stock_a, manager_with_pin, tenant_a
    ):
        client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "peter",
                    "pin": "9137",
                    "reason": "Because",
                },
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            entries = list(AuditLog.objects.all())

        assert "9137" not in str([(e.before, e.after, e.reason) for e in entries])

    def test_a_successful_authorization_is_recorded_too(
        self, client_cashier_a, item_a, stock_a, manager_with_pin, tenant_a
    ):
        """The trail should read: this person rang it up, that person approved."""
        client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                cart_discount_cents=1000,
                discount_authorization={
                    "username": "peter",
                    "pin": "4471",
                    "reason": "Damaged packaging",
                },
            ),
            format="json",
        )

        with transaction.atomic(), tenant_context(tenant_a.id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_AUTHORIZED).first()

        assert entry is not None
        assert entry.actor_label == "mary"  # the cashier
        assert entry.after["authorized_by"] == "peter"  # the manager
        assert entry.reason == "Damaged packaging"


class TestPricesAtThisLayer:
    def test_a_supplied_price_on_a_fixed_item_is_still_ignored(
        self, client_cashier_a, item_a, stock_a
    ):
        """The service already refuses this. Proving the endpoint adds no
        second route around it: a guard at one layer is worth little if another
        layer quietly offers a way past.
        """
        response = client_cashier_a.post(
            CHECKOUT,
            body(
                item_a,
                lines=[
                    {"item_id": str(item_a.id), "quantity": "1", "unit_price_cents": 100}
                ],
            ),
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["total_cents"] == 18000
        assert response.json()["lines"][0]["unit_price_cents"] == 18000


class TestCashRounding:
    def test_a_total_that_is_not_a_whole_shilling_is_rounded(
        self, client_manager_a, tenant_a, store_a, tax_rate_a, stock_a
    ):
        """No coin below KES 1 circulates, so the tender rounds and the
        difference is recorded - otherwise a drawer drifts a few shillings a day
        and nobody can say why.
        """
        from apps.catalog.models import Item

        with transaction.atomic(), tenant_context(tenant_a.id):
            odd = Item.objects.create(
                tenant=tenant_a,
                sku="ODD-1",
                name="Odd priced",
                price_cents=18749,  # KES 187.49
                tax_rate=tax_rate_a,
                track_stock=False,
            )

        response = client_manager_a.post(
            CHECKOUT,
            {
                "lines": [{"item_id": str(odd.id), "quantity": "1"}],
                "tendered_cents": 20000,
            },
            format="json",
        )
        assert response.status_code == 201, response.content

        sale = response.json()
        assert sale["total_cents"] == 18749
        # 187.49 rounds down to 187.00, so the shop takes one cent less.
        assert sale["rounding_adjustment_cents"] == -49
        assert sale["amount_due_cents"] == 18700
        assert sale["change_cents"] == 1300

    def test_rounding_up_at_the_half_shilling(
        self, client_manager_a, tenant_a, store_a, tax_rate_a
    ):
        from apps.catalog.models import Item

        with transaction.atomic(), tenant_context(tenant_a.id):
            odd = Item.objects.create(
                tenant=tenant_a,
                sku="ODD-2",
                name="Odd priced two",
                price_cents=18750,  # KES 187.50
                tax_rate=tax_rate_a,
                track_stock=False,
            )

        response = client_manager_a.post(
            CHECKOUT,
            {
                "lines": [{"item_id": str(odd.id), "quantity": "1"}],
                "tendered_cents": 20000,
            },
            format="json",
        )
        sale = response.json()

        assert sale["rounding_adjustment_cents"] == 50
        assert sale["amount_due_cents"] == 18800

    def test_a_whole_shilling_total_is_left_alone(
        self, client_cashier_a, item_a, stock_a
    ):
        response = client_cashier_a.post(CHECKOUT, body(item_a), format="json")
        assert response.json()["rounding_adjustment_cents"] == 0


class TestCheckoutRefusals:
    def test_short_payment_is_refused(self, client_cashier_a, item_a, stock_a):
        response = client_cashier_a.post(
            CHECKOUT, body(item_a, tendered_cents=1000), format="json"
        )
        assert response.status_code == 400
        assert response.json()["code"] == "insufficient_tender"

    def test_an_empty_cart_is_refused(self, client_cashier_a):
        response = client_cashier_a.post(
            CHECKOUT, {"lines": [], "tendered_cents": 1000}, format="json"
        )
        assert response.status_code == 400

    def test_another_businesss_item_is_refused(self, client_cashier_a, item_b, store_a):
        response = client_cashier_a.post(CHECKOUT, body(item_b), format="json")
        assert response.status_code == 400
        assert response.json()["code"] == "unknown_item"

    def test_sales_are_not_visible_across_businesses(
        self, client_cashier_a, client_owner_b, item_a, stock_a
    ):
        client_cashier_a.post(CHECKOUT, body(item_a), format="json")

        assert client_owner_b.get("/api/v1/sales/").json()["results"] == []


@pytest.mark.django_db
class TestCashRoundingSettlesTheSale:
    """A total that is not a whole shilling must still finish the sale.

    Regression. ``ledger_position`` used to read ``total_cents`` raw while
    ``take_cash`` charged the *rounded* figure, so the two disagreed by up to
    fifty cents and the sale never reached PAID: the customer's money was
    taken, no receipt number was allocated and the stock never moved. Almost
    every real sale hits this, because a VAT-inclusive price rarely lands on a
    whole shilling.

    ``initiate_stk`` had been patched around it locally, which is what a
    missing concept looks like from the inside.
    """

    def _sell_at(self, client, cashier, item, price_cents, tendered):
        with tenant_context(cashier.tenant_id):
            item.price_cents = price_cents
            item.save()
        return client.post(
            CHECKOUT,
            {
                "lines": [{"item_id": str(item.id), "quantity": "1"}],
                "tendered_cents": tendered,
            },
            format="json",
        )

    def test_a_total_rounded_down_still_settles(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        body = self._sell_at(client_cashier_a, cashier_a, item_a, 18049, 18000).json()

        assert body["state"] == SaleState.PAID
        assert body["receipt_number"] is not None
        assert body["rounding_adjustment_cents"] == -49

    def test_a_total_rounded_up_still_settles(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """Asserts settlement, not just the arithmetic.

        Rounding *up* is the case that hid the original bug. The extra cents
        pushed the ledger past the raw total, so the sale reached PAID by
        accident while being wrongly marked overpaid - and a test checking only
        the state and the adjustment passed throughout. What pins it is that the
        ledger is exactly covered: no more, no less.
        """
        body = self._sell_at(client_cashier_a, cashier_a, item_a, 18051, 18100).json()

        assert body["state"] == SaleState.PAID
        assert body["receipt_number"] is not None
        assert body["rounding_adjustment_cents"] == 49
        assert body["is_overpaid"] is False
        assert body["amount_due_cents"] == 18100
        assert sum(p["amount_cents"] for p in body["payments"]) == 18100
        assert body["change_cents"] == 0

    def test_the_payment_ledger_exactly_covers_a_rounded_down_sale(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """The same point from the rounding-down side.

        The state assertion is not decoration. Every other figure here was
        already correct while the bug was live - the amount due, the payment,
        the overpaid flag - and the only thing wrong was that the sale sat at
        OPEN forever. Ledger arithmetic alone would have passed.
        """
        body = self._sell_at(client_cashier_a, cashier_a, item_a, 18049, 18000).json()

        assert body["state"] == SaleState.PAID
        assert body["amount_due_cents"] == 18000
        assert sum(p["amount_cents"] for p in body["payments"]) == 18000
        assert body["is_overpaid"] is False

    def test_the_stock_moves_on_a_rounded_sale(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        self._sell_at(client_cashier_a, cashier_a, item_a, 18049, 18000)

        with tenant_context(cashier_a.tenant_id):
            assert StockItem.objects.get(pk=stock_a.pk).quantity == 39

    def test_a_rounded_sale_is_not_reported_as_overpaid(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        """Rounding up means the customer pays a few cents more than the goods
        listed at. That is the rounding, not an overpayment to refund."""
        body = self._sell_at(client_cashier_a, cashier_a, item_a, 18051, 18100).json()

        assert body["is_overpaid"] is False
