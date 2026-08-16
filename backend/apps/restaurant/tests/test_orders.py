"""
Tables, orders, modifiers and tickets.

The cases worth testing are the ones where a restaurant differs from a duka:
food that has already been cooked, a kitchen that must not be told twice, and a
table whose bill has to stay answerable after it was merged into another.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context
from apps.restaurant.models import (
    KitchenTicket,
    Order,
    OrderLine,
    OrderState,
    Table,
)
from apps.restaurant.services import (
    OrderError,
    add_line,
    merge_orders,
    move_order,
    open_order,
    void_order,
)
from apps.sales.models import Sale, SaleState

ORDERS = "/api/v1/restaurant/orders/"
TABLES = "/api/v1/restaurant/tables/"
TICKETS = "/api/v1/restaurant/kitchen-tickets/"


def open_via_api(client, table) -> dict:
    return client.post(
        ORDERS, {"table_id": str(table.id), "covers": 2}, format="json"
    ).json()


def add_via_api(client, order, item, *, quantity="1", modifiers=None, note=""):
    return client.post(
        f"{ORDERS}{order['id']}/lines/",
        {
            "item_id": str(item.id),
            "quantity": quantity,
            "modifier_ids": [str(m) for m in (modifiers or [])],
            "note": note,
        },
        format="json",
    )


@pytest.mark.django_db
class TestTheModuleBoundary:
    def test_a_duka_cannot_reach_any_of_it(
        self, client_cashier_a, tenant_a, store_a
    ):
        """A retail business must not acquire tables because somebody found the
        URL. The module is a data question, not a routing one."""
        for url in (TABLES, ORDERS, TICKETS):
            response = client_cashier_a.get(url)
            assert response.status_code == 400, url
            assert response.json()["code"] == "module_not_enabled"

    def test_a_restaurant_can(self, client_cashier_a, restaurant, store_a):
        assert client_cashier_a.get(TABLES).status_code == 200

    def test_the_service_refuses_too_not_only_the_view(self, tenant_a, store_a, cashier_a):
        with tenant_context(tenant_a.id):
            with pytest.raises(OrderError) as exc:
                open_order(
                    tenant=tenant_a, store=store_a, table=None, user=cashier_a
                )
        assert exc.value.code == "module_not_enabled"


@pytest.mark.django_db
class TestOpeningATable:
    def test_a_waiter_opens_an_order(self, client_cashier_a, table_four):
        response = client_cashier_a.post(
            ORDERS, {"table_id": str(table_four.id), "covers": 2}, format="json"
        )

        assert response.status_code == 201
        assert response.json()["state"] == OrderState.OPEN
        assert response.json()["table_name"] == "Table 4"

    def test_one_live_order_per_table(self, client_cashier_a, table_four):
        """Two would make 'what does table four owe' unanswerable, and that
        question is the entire point of the record."""
        open_via_api(client_cashier_a, table_four)

        second = client_cashier_a.post(
            ORDERS, {"table_id": str(table_four.id)}, format="json"
        )

        assert second.status_code == 400
        assert second.json()["code"] == "table_occupied"

    def test_a_second_table_is_fine(self, client_cashier_a, table_four, table_six):
        open_via_api(client_cashier_a, table_four)

        assert (
            client_cashier_a.post(
                ORDERS, {"table_id": str(table_six.id)}, format="json"
            ).status_code
            == 201
        )

    def test_a_table_frees_up_once_billed(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        )

        assert (
            client_cashier_a.post(
                ORDERS, {"table_id": str(table_four.id)}, format="json"
            ).status_code
            == 201
        )

    def test_a_retired_table_cannot_be_used(
        self, client_cashier_a, restaurant, table_four
    ):
        with tenant_context(restaurant.id):
            Table.objects.filter(pk=table_four.pk).update(is_active=False)

        response = client_cashier_a.post(
            ORDERS, {"table_id": str(table_four.id)}, format="json"
        )

        assert response.json()["code"] == "table_inactive"

    def test_the_floor_shows_which_tables_are_occupied(
        self, client_cashier_a, table_four, table_six
    ):
        open_via_api(client_cashier_a, table_four)

        tables = {row["name"]: row for row in client_cashier_a.get(TABLES).json()["results"]}

        assert tables["Table 4"]["is_occupied"] is True
        assert tables["Table 6"]["is_occupied"] is False


@pytest.mark.django_db
class TestAddingToAnOrder:
    def test_a_line_is_added(self, client_cashier_a, table_four, soda):
        order = open_via_api(client_cashier_a, table_four)

        response = add_via_api(client_cashier_a, order, soda)

        assert response.status_code == 201
        assert response.json()["name"] == "Soda"

    def test_the_price_is_frozen_at_the_moment_it_was_ordered(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        """A price change during service must not rewrite what a table was
        quoted an hour ago."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)

        with tenant_context(restaurant.id):
            soda.price_cents = 99999
            soda.save()
            line = OrderLine.objects.get()

        assert line.base_price_cents == 15000

    def test_pudding_can_be_added_after_the_mains_have_gone_out(
        self, client_cashier_a, table_four, steak, soda, doneness, rare
    ):
        """The ordinary case, not an exception."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, steak, modifiers=[rare.id])
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        response = add_via_api(client_cashier_a, order, soda)

        assert response.status_code == 201

    def test_a_closed_order_cannot_be_added_to(
        self, client_cashier_a, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Walked out"}, format="json"
        )

        response = add_via_api(client_cashier_a, order, soda)

        assert response.json()["code"] == "order_not_live"

    def test_a_zero_quantity_is_refused(self, client_cashier_a, table_four, soda):
        order = open_via_api(client_cashier_a, table_four)

        assert add_via_api(client_cashier_a, order, soda, quantity="0").status_code == 400

    def test_a_kitchen_note_rides_along(
        self, client_cashier_a, table_four, steak, doneness, rare
    ):
        order = open_via_api(client_cashier_a, table_four)

        response = add_via_api(
            client_cashier_a,
            order,
            steak,
            modifiers=[rare.id],
            note="Allergy - nuts",
        )

        assert response.json()["note"] == "Allergy - nuts"


@pytest.mark.django_db
class TestModifiers:
    def test_a_required_choice_is_enforced(
        self, client_cashier_a, table_four, steak, doneness, rare
    ):
        """A steak with no doneness is an order nobody can cook, and finding
        that out at the pass is worse than finding it out at the table."""
        order = open_via_api(client_cashier_a, table_four)

        response = add_via_api(client_cashier_a, order, steak)

        assert response.status_code == 400
        assert response.json()["code"] == "modifier_required"

    def test_choosing_one_satisfies_it(
        self, client_cashier_a, table_four, steak, doneness, rare
    ):
        order = open_via_api(client_cashier_a, table_four)

        response = add_via_api(client_cashier_a, order, steak, modifiers=[rare.id])

        assert response.status_code == 201
        assert response.json()["modifiers"][0]["name"] == "Rare"

    def test_too_many_choices_are_refused(
        self, client_cashier_a, table_four, steak, doneness, rare, medium
    ):
        order = open_via_api(client_cashier_a, table_four)
        

        response = add_via_api(
            client_cashier_a, order, steak, modifiers=[rare.id, medium.id]
        )

        assert response.json()["code"] == "too_many_modifiers"

    def test_an_optional_group_needs_nothing(
        self, client_cashier_a, table_four, steak, doneness, extras, rare, chilli, no_onions
    ):
        order = open_via_api(client_cashier_a, table_four)

        assert (
            add_via_api(client_cashier_a, order, steak, modifiers=[rare.id]).status_code
            == 201
        )

    def test_a_free_modifier_costs_nothing(
        self, client_cashier_a, table_four, steak, doneness, extras, rare, chilli, no_onions
    ):
        order = open_via_api(client_cashier_a, table_four)

        response = add_via_api(
            client_cashier_a, order, steak, modifiers=[rare.id, no_onions.id]
        )

        assert response.json()["unit_price_cents"] == 120000

    def test_a_priced_modifier_shows_on_the_running_total(
        self, client_cashier_a, table_four, steak, doneness, extras, rare, chilli, no_onions
    ):
        order = open_via_api(client_cashier_a, table_four)

        response = add_via_api(
            client_cashier_a, order, steak, modifiers=[rare.id, chilli.id]
        )

        assert response.json()["unit_price_cents"] == 122000

    def test_the_chosen_name_and_price_are_frozen(
        self, client_cashier_a, restaurant, table_four, steak, doneness, extras, rare, chilli
    ):
        """Renaming 'extra chilli' next week must not restate tonight's bill."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, steak, modifiers=[rare.id, chilli.id])

        with tenant_context(restaurant.id):
            chilli.name = "Chilli (extra hot)"
            chilli.price_cents = 9999
            chilli.save()
            chosen = OrderLine.objects.get().modifiers.get(price_cents__gt=0)

        assert chosen.name == "Extra chilli"
        assert chosen.price_cents == 2000

    def test_a_priced_modifier_must_be_sellable(self, restaurant):
        """It bills as a catalogue line, so it has to point at one. Without
        that, create_sale would ignore the price and the surcharge would
        silently vanish."""
        from django.db.utils import IntegrityError

        from apps.restaurant.models import Modifier, ModifierGroup

        with tenant_context(restaurant.id):
            group = ModifierGroup.objects.create(tenant=restaurant, name="Sides")
            with pytest.raises(IntegrityError):
                Modifier.objects.create(
                    tenant=restaurant, group=group, name="Chips", price_cents=5000
                )

    def test_groups_can_be_listed_for_one_item(
        self, client_cashier_a, steak, doneness, extras
    ):
        response = client_cashier_a.get(
            "/api/v1/restaurant/modifier-groups/", {"item": str(steak.id)}
        )

        names = {row["name"] for row in response.json()["results"]}
        assert names == {"How would you like it", "Extras"}


@pytest.mark.django_db
class TestKitchenTickets:
    def test_sending_prints_what_is_on_the_order(
        self, client_cashier_a, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/send/", {}, format="json"
        )

        assert response.status_code == 201
        assert response.json()["sequence"] == 1
        assert len(response.json()["lines"]) == 1

    def test_a_second_ticket_carries_only_what_is_new(
        self, client_cashier_a, table_four, steak, soda, doneness, rare
    ):
        """The failure this exists to prevent: a waiter adding two drinks
        mid-meal must not have the kitchen cook the whole table again."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, steak, modifiers=[rare.id])
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        add_via_api(client_cashier_a, order, soda)
        second = client_cashier_a.post(
            f"{ORDERS}{order['id']}/send/", {}, format="json"
        ).json()

        assert second["sequence"] == 2
        assert [line["name"] for line in second["lines"]] == ["Soda"]

    def test_sending_with_nothing_new_is_refused(
        self, client_cashier_a, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        again = client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        assert again.json()["code"] == "nothing_to_send"

    def test_the_order_moves_to_sent(self, client_cashier_a, table_four, soda):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        assert client_cashier_a.get(f"{ORDERS}{order['id']}/").json()["state"] == (
            OrderState.SENT
        )

    def test_a_voided_line_is_not_sent(
        self, client_cashier_a, restaurant, table_four, steak, soda, doneness, rare
    ):
        order = open_via_api(client_cashier_a, table_four)
        line = add_via_api(client_cashier_a, order, steak, modifiers=[rare.id]).json()
        add_via_api(client_cashier_a, order, soda)

        client_cashier_a.post(
            f"{ORDERS}{order['id']}/lines/{line['id']}/void/",
            {"reason": "Changed their mind"},
            format="json",
        )
        ticket = client_cashier_a.post(
            f"{ORDERS}{order['id']}/send/", {}, format="json"
        ).json()

        assert [row["name"] for row in ticket["lines"]] == ["Soda"]

    def test_a_reprint_is_that_ticket_not_everything_new(
        self, client_cashier_a, restaurant, table_four, steak, soda, doneness, rare
    ):
        """A reprint that behaved like a fresh send would have the kitchen cook
        a different set of food from the one the waiter asked for."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, steak, modifiers=[rare.id])
        first = client_cashier_a.post(
            f"{ORDERS}{order['id']}/send/", {}, format="json"
        ).json()
        add_via_api(client_cashier_a, order, soda)

        reprint = client_cashier_a.post(
            f"{TICKETS}{first['id']}/reprint/", {}, format="json"
        ).json()

        assert [row["name"] for row in reprint["lines"]] == ["Sirloin steak"]
        assert reprint["reprint_count"] == 1

    def test_tickets_are_numbered_per_order(
        self, client_cashier_a, table_four, table_six, soda
    ):
        first = open_via_api(client_cashier_a, table_four)
        second = open_via_api(client_cashier_a, table_six)
        for order in (first, second):
            add_via_api(client_cashier_a, order, soda)
            client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        with tenant_context(soda.tenant_id):
            assert sorted(KitchenTicket.objects.values_list("sequence", flat=True)) == [1, 1]


@pytest.mark.django_db
class TestVoidingBeforeTheKitchenKnows:
    def test_a_waiter_may_cancel_freely(self, client_cashier_a, table_four, soda):
        """Nothing has been cooked, so it costs the restaurant nothing."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/",
            {"reason": "Customer left"},
            format="json",
        )

        assert response.status_code == 200
        assert response.json()["state"] == OrderState.VOID

    def test_no_authority_is_asked_for(self, client_cashier_a, table_four, soda):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Left"}, format="json"
        )

        assert response.status_code == 200

    def test_it_is_recorded_as_needing_none(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Left"}, format="json"
        )

        with tenant_context(restaurant.id):
            entry = AuditLog.objects.filter(action=AuditAction.VOID).first()

        assert entry.after["via"] == "NOT_REQUIRED"
        assert entry.after["sent_line_count"] == 0


@pytest.mark.django_db
class TestVoidingAfterTheKitchenHasCooked:
    """The ingredients are spent, and the void makes that disappear. Same shape
    as a discount, same mechanism."""

    def _sent_order(self, client, table, item, modifiers=None):
        order = open_via_api(client, table)
        add_via_api(client, order, item, modifiers=modifiers)
        client.post(f"{ORDERS}{order['id']}/send/", {}, format="json")
        return order

    def test_a_cashier_is_refused_without_a_manager(
        self, client_cashier_a, table_four, soda
    ):
        order = self._sent_order(client_cashier_a, table_four, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Sent back"}, format="json"
        )

        assert response.status_code == 403
        assert response.json()["code"] == "discount_authorization_required"

    def test_a_manager_authorises_from_their_own_session(
        self, client_manager_a, table_four, soda
    ):
        """Their session already carries that authority; asking them to prove it
        again would be a ritual."""
        order = self._sent_order(client_manager_a, table_four, soda)

        response = client_manager_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Sent back"}, format="json"
        )

        assert response.status_code == 200

    def test_a_cashier_may_delegate_to_a_present_manager(
        self, client_cashier_a, restaurant, table_four, soda, manager_a
    ):
        with tenant_context(restaurant.id):
            manager_a.set_pin("4455")
            manager_a.save()

        order = self._sent_order(client_cashier_a, table_four, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/",
            {"reason": "Sent back", "username": "mngr", "pin": "4455"},
            format="json",
        )

        assert response.status_code == 200

    def test_a_wrong_pin_is_refused(
        self, client_cashier_a, restaurant, table_four, soda, manager_a
    ):
        with tenant_context(restaurant.id):
            manager_a.set_pin("4455")
            manager_a.save()

        order = self._sent_order(client_cashier_a, table_four, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/",
            {"reason": "Sent back", "username": "mngr", "pin": "0000"},
            format="json",
        )

        assert response.status_code == 403

    def test_a_refusal_is_audited_as_a_void_not_a_discount(
        self, client_cashier_a, restaurant, table_four, soda, manager_a
    ):
        """A refused void must not appear in the history as a refused
        discount - the same mechanism, filed under its own name."""
        order = self._sent_order(client_cashier_a, table_four, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/",
            {"reason": "Sent back", "username": "mngr", "pin": "0000"},
            format="json",
        )

        with tenant_context(restaurant.id):
            assert AuditLog.objects.filter(
                action=AuditAction.ORDER_VOID_REFUSED
            ).exists()
            assert not AuditLog.objects.filter(
                action=AuditAction.DISCOUNT_REFUSED
            ).exists()

    def test_a_refused_void_leaves_the_order_open(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = self._sent_order(client_cashier_a, table_four, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Sent back"}, format="json"
        )

        with tenant_context(restaurant.id):
            assert Order.objects.get(pk=order["id"]).state == OrderState.SENT

    def test_what_the_kitchen_had_already_made_is_recorded(
        self, client_manager_a, restaurant, table_four, steak, soda, doneness, rare
    ):
        """The first question anybody asks afterwards."""
        order = open_via_api(client_manager_a, table_four)
        add_via_api(client_manager_a, order, steak, modifiers=[rare.id])
        add_via_api(client_manager_a, order, soda, quantity="2")
        client_manager_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        client_manager_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Kitchen fire"}, format="json"
        )

        with tenant_context(restaurant.id):
            entry = AuditLog.objects.filter(
                action=AuditAction.VOID, entity_type="restaurant.Order"
            ).first()

        assert entry.after["sent_line_count"] == 2
        names = {row["item"] for row in entry.after["lines_already_sent"]}
        assert names == {"Sirloin steak", "Soda"}

    def test_a_reason_is_always_required(
        self, client_manager_a, table_four, soda
    ):
        order = self._sent_order(client_manager_a, table_four, soda)

        response = client_manager_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "   "}, format="json"
        )

        assert response.status_code in (400, 403)


@pytest.mark.django_db
class TestStrikingALineOff:
    def test_a_line_is_kept_rather_than_deleted(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        """A line that reached the kitchen and was cancelled is a thing
        somebody may have to explain."""
        order = open_via_api(client_cashier_a, table_four)
        line = add_via_api(client_cashier_a, order, soda).json()

        client_cashier_a.post(
            f"{ORDERS}{order['id']}/lines/{line['id']}/void/",
            {"reason": "Wrong table"},
            format="json",
        )

        with tenant_context(restaurant.id):
            stored = OrderLine.objects.get(pk=line["id"])

        assert stored.is_voided is True
        assert stored.void_reason == "Wrong table"

    def test_a_reason_is_required(self, client_cashier_a, table_four, soda):
        order = open_via_api(client_cashier_a, table_four)
        line = add_via_api(client_cashier_a, order, soda).json()

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/lines/{line['id']}/void/",
            {"reason": "  "},
            format="json",
        )

        assert response.status_code == 400

    def test_whether_it_had_reached_the_kitchen_is_recorded(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        line = add_via_api(client_cashier_a, order, soda).json()
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        client_cashier_a.post(
            f"{ORDERS}{order['id']}/lines/{line['id']}/void/",
            {"reason": "Sent back"},
            format="json",
        )

        with tenant_context(restaurant.id):
            entry = AuditLog.objects.filter(
                action=AuditAction.VOID, entity_type="restaurant.OrderLine"
            ).first()

        assert entry.after["had_been_sent"] is True


@pytest.mark.django_db
class TestMovingAndMerging:
    def test_an_order_moves_to_another_table(
        self, client_cashier_a, table_four, table_six, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/move/",
            {"table_id": str(table_six.id)},
            format="json",
        )

        assert response.json()["table_name"] == "Table 6"

    def test_a_move_is_recorded_with_both_tables(
        self, client_cashier_a, restaurant, table_four, table_six, soda
    ):
        """'Table four moved to the terrace at eight' is what a manager asks
        when a bill goes to the wrong party."""
        order = open_via_api(client_cashier_a, table_four)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/move/",
            {"table_id": str(table_six.id)},
            format="json",
        )

        with tenant_context(restaurant.id):
            entry = AuditLog.objects.filter(
                action=AuditAction.UPDATE, entity_type="restaurant.Order"
            ).first()

        assert entry.before["table"] == "Table 4"
        assert entry.after["table"] == "Table 6"

    def test_moving_onto_an_occupied_table_is_refused(
        self, client_cashier_a, table_four, table_six, soda
    ):
        first = open_via_api(client_cashier_a, table_four)
        open_via_api(client_cashier_a, table_six)

        response = client_cashier_a.post(
            f"{ORDERS}{first['id']}/move/",
            {"table_id": str(table_six.id)},
            format="json",
        )

        assert response.json()["code"] == "table_occupied"

    def test_merging_moves_the_lines(
        self, client_cashier_a, restaurant, table_four, table_six, soda, steak, doneness, rare
    ):
        source = open_via_api(client_cashier_a, table_four)
        target = open_via_api(client_cashier_a, table_six)
        add_via_api(client_cashier_a, source, soda)
        add_via_api(client_cashier_a, target, soda)

        response = client_cashier_a.post(
            f"{ORDERS}{source['id']}/merge/",
            {"into_order_id": target["id"]},
            format="json",
        )

        assert len(response.json()["lines"]) == 2

    def test_the_emptied_order_survives_as_a_record(
        self, client_cashier_a, restaurant, table_four, table_six, soda
    ):
        """Deleting it would make a bill somebody queried simply not exist."""
        source = open_via_api(client_cashier_a, table_four)
        target = open_via_api(client_cashier_a, table_six)
        add_via_api(client_cashier_a, source, soda)

        client_cashier_a.post(
            f"{ORDERS}{source['id']}/merge/",
            {"into_order_id": target["id"]},
            format="json",
        )

        with tenant_context(restaurant.id):
            emptied = Order.objects.get(pk=source["id"])

        assert emptied.state == OrderState.MERGED
        assert str(emptied.merged_into_id) == target["id"]
        assert emptied.table_id is None

    def test_the_source_table_frees_up(
        self, client_cashier_a, table_four, table_six, soda
    ):
        source = open_via_api(client_cashier_a, table_four)
        target = open_via_api(client_cashier_a, table_six)
        add_via_api(client_cashier_a, source, soda)
        client_cashier_a.post(
            f"{ORDERS}{source['id']}/merge/",
            {"into_order_id": target["id"]},
            format="json",
        )

        assert (
            client_cashier_a.post(
                ORDERS, {"table_id": str(table_four.id)}, format="json"
            ).status_code
            == 201
        )

    def test_an_order_cannot_be_merged_into_itself(
        self, client_cashier_a, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/merge/",
            {"into_order_id": order["id"]},
            format="json",
        )

        assert response.json()["code"] == "same_order"


@pytest.mark.django_db
class TestBilling:
    def test_an_order_becomes_an_ordinary_sale(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        )

        assert response.status_code == 201
        assert response.json()["state"] == SaleState.PAID
        assert response.json()["receipt_number"] is not None

    def test_the_order_closes(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        )

        with tenant_context(restaurant.id):
            stored = Order.objects.get(pk=order["id"])

        assert stored.state == OrderState.BILLED
        assert stored.sale_id is not None

    def test_a_priced_modifier_bills_as_its_own_line(
        self, client_cashier_a, restaurant, table_four, steak, doneness, extras, rare, chilli
    ):
        """create_sale ignores a client-supplied price, and this module is not
        going to weaken that to fold a surcharge into a dish price."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, steak, modifiers=[rare.id, chilli.id])

        body = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 122000}, format="json"
        ).json()

        names = [line["name"] for line in body["lines"]]
        assert names == ["Sirloin steak", "Extra chilli"]
        assert body["total_cents"] == 122000

    def test_two_dishes_bring_two_modifier_lines(
        self, client_cashier_a, restaurant, table_four, steak, doneness, extras, rare, chilli
    ):
        """Two steaks with extra chilli is two chillies, not one."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(
            client_cashier_a, order, steak, quantity="2", modifiers=[rare.id, chilli.id]
        )

        body = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 244000}, format="json"
        ).json()

        assert body["total_cents"] == 244000

    def test_a_free_modifier_produces_no_line(
        self, client_cashier_a, restaurant, table_four, steak, doneness, extras, rare, no_onions
    ):
        """The kitchen was told; the till has nothing to charge for."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(
            client_cashier_a, order, steak, modifiers=[rare.id, no_onions.id]
        )

        body = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 120000}, format="json"
        ).json()

        assert len(body["lines"]) == 1

    def test_a_struck_off_line_is_not_billed(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        keep = add_via_api(client_cashier_a, order, soda).json()
        drop = add_via_api(client_cashier_a, order, soda).json()
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/lines/{drop['id']}/void/",
            {"reason": "Not wanted"},
            format="json",
        )

        body = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        ).json()

        assert body["total_cents"] == 15000
        assert keep["id"] != drop["id"]

    def test_an_empty_order_cannot_be_billed(
        self, client_cashier_a, table_four
    ):
        order = open_via_api(client_cashier_a, table_four)

        response = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 0}, format="json"
        )

        assert response.json()["code"] == "empty_order"

    def test_billing_twice_is_refused(
        self, client_cashier_a, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        )

        again = client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        )

        assert again.json()["code"] == "order_not_live"


@pytest.mark.django_db
class TestAnOpenOrderIsNotRevenue:
    def test_it_appears_in_no_report(
        self, client_manager_a, client_cashier_a, restaurant, table_four, soda
    ):
        """The reporting layer filters on Sale.state and never sees an order at
        all, which is why it needed no change."""
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(f"{ORDERS}{order['id']}/send/", {}, format="json")

        body = client_manager_a.get("/api/v1/reports/sales/").json()

        assert body["periods"][0]["sale_count"] == 0
        assert body["periods"][0]["gross_cents"] == 0

    def test_it_creates_no_sale_until_billed(
        self, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)

        with tenant_context(restaurant.id):
            assert Sale.objects.count() == 0

    def test_it_appears_once_billed(
        self, client_manager_a, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/bill/", {"tendered_cents": 15000}, format="json"
        )

        body = client_manager_a.get("/api/v1/reports/sales/").json()

        assert body["periods"][0]["sale_count"] == 1
        assert body["periods"][0]["gross_cents"] == 15000

    def test_a_voided_order_never_appears(
        self, client_manager_a, client_cashier_a, restaurant, table_four, soda
    ):
        order = open_via_api(client_cashier_a, table_four)
        add_via_api(client_cashier_a, order, soda)
        client_cashier_a.post(
            f"{ORDERS}{order['id']}/void/", {"reason": "Left"}, format="json"
        )

        body = client_manager_a.get("/api/v1/reports/sales/").json()

        assert body["periods"][0]["sale_count"] == 0
        # Not counted as a sale void either - no Sale was ever created.
        assert body["periods"][0]["void_count"] == 0


@pytest.mark.django_db
class TestTheStateMachine:
    def test_a_billed_order_cannot_be_voided(
        self, tenant_a, restaurant, store_a, cashier_a, table_four, soda
    ):
        from apps.restaurant.services import close_order

        with tenant_context(restaurant.id):
            order = open_order(
                tenant=restaurant, store=store_a, table=table_four, user=cashier_a
            )
            add_line(order=order, item_id=str(soda.id), quantity=Decimal("1"))
            close_order(order=order)

            with pytest.raises(OrderError) as exc:
                void_order(order=order, user=cashier_a, reason="Too late")

        assert exc.value.code == "order_not_live"

    def test_a_voided_order_cannot_be_merged(
        self, restaurant, store_a, cashier_a, table_four, table_six, soda
    ):
        with tenant_context(restaurant.id):
            source = open_order(
                tenant=restaurant, store=store_a, table=table_four, user=cashier_a
            )
            target = open_order(
                tenant=restaurant, store=store_a, table=table_six, user=cashier_a
            )
            void_order(order=source, user=cashier_a, reason="Left")

            with pytest.raises(OrderError) as exc:
                merge_orders(source=source, target=target, user=cashier_a)

        assert exc.value.code == "order_not_live"

    def test_a_merged_order_cannot_be_moved(
        self, restaurant, store_a, cashier_a, table_four, table_six, soda
    ):
        with tenant_context(restaurant.id):
            source = open_order(
                tenant=restaurant, store=store_a, table=table_four, user=cashier_a
            )
            target = open_order(
                tenant=restaurant, store=store_a, table=table_six, user=cashier_a
            )
            add_line(order=source, item_id=str(soda.id), quantity=Decimal("1"))
            merge_orders(source=source, target=target, user=cashier_a)

            with pytest.raises(OrderError) as exc:
                move_order(order=source, table=table_four, user=cashier_a)

        assert exc.value.code == "order_not_live"


@pytest.mark.django_db
class TestItStaysInsideOneBusiness:
    def test_another_restaurant_sees_no_tables(
        self, client_cashier_a, restaurant, table_four, tenant_b, client_owner_b, store_b
    ):
        from apps.tenants.models import ModuleKey, TenantModule

        with tenant_context(tenant_b.id):
            TenantModule.objects.update_or_create(
                tenant=tenant_b,
                module_key=ModuleKey.RESTAURANT,
                defaults={"is_enabled": True},
            )

        assert client_owner_b.get(TABLES).json()["results"] == []

    def test_another_restaurant_sees_no_orders(
        self,
        client_cashier_a,
        restaurant,
        table_four,
        soda,
        tenant_b,
        client_owner_b,
        store_b,
    ):
        from apps.tenants.models import ModuleKey, TenantModule

        with tenant_context(tenant_b.id):
            TenantModule.objects.update_or_create(
                tenant=tenant_b,
                module_key=ModuleKey.RESTAURANT,
                defaults={"is_enabled": True},
            )
        open_via_api(client_cashier_a, table_four)

        assert client_owner_b.get(ORDERS).json()["results"] == []

    def test_a_table_name_may_repeat_across_businesses(
        self, restaurant, table_four, tenant_b, store_b
    ):
        """Two restaurants both having a 'Table 4' is normal."""
        with tenant_context(tenant_b.id):
            Table.objects.create(tenant=tenant_b, store=store_b, name="Table 4")

        with tenant_context(tenant_b.id):
            assert Table.objects.filter(name="Table 4").count() == 1

    def test_orders_need_authentication(self, anon_client):
        assert anon_client.get(ORDERS).status_code == 401
