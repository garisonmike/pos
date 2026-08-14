"""
Uploading a till's backlog.

The questions worth asking here are all about what happens the *second* time a
sale arrives, or when it arrives claiming something the server can check and
the till could not.
"""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.accounts.constants import UserRole
from apps.accounts.models import Device, User
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import bypass_rls, tenant_context
from apps.inventory.models import StockItem
from apps.sales.authorization import AuthorizationMethod
from apps.sales.models import Sale, SaleDiscrepancy, SaleState

SYNC = "/api/v1/sync/sales/"


def offline_sale(item, *, client_uuid=None, tendered=18000, **overrides) -> dict:
    """One sale as a till would have stored it."""
    body = {
        "client_uuid": str(client_uuid or uuid.uuid4()),
        "device_created_at": timezone.now().isoformat(),
        "lines": [{"item_id": str(item.id), "quantity": "1"}],
        "tendered_cents": tendered,
    }
    body.update(overrides)
    return body


def batch(device, sales, refusals=None) -> dict:
    return {
        "device_id": str(device.id),
        "sales": sales,
        "refused_authorizations": refusals or [],
    }


@pytest.fixture
def manager_with_pin(tenant_a) -> User:
    """A manager whose PIN a till could have cached."""
    with bypass_rls():
        user = User(
            tenant=tenant_a,
            username="grace",
            full_name="Grace Manager",
            role=UserRole.MANAGER,
        )
        user.set_password("staff-pass-4471")
        user.set_pin("4455")
        user.save()
        return user


@pytest.mark.django_db
class TestAcceptingABacklog:
    def test_an_offline_sale_lands_as_a_paid_sale(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a)]), format="json"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert body["results"][0]["status"] == "accepted"
        assert body["results"][0]["receipt_number"] is not None

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=body["results"][0]["sale_id"])
            assert sale.state == SaleState.PAID

    def test_the_sale_is_marked_as_having_been_rung_up_offline(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        rung_up_at = timezone.now() - timezone.timedelta(hours=6)
        response = client_cashier_a.post(
            SYNC,
            batch(
                device,
                [
                    offline_sale(
                        item_a,
                        device_created_at=rung_up_at.isoformat(),
                        device_sequence=17,
                    )
                ],
            ),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=response.json()["results"][0]["sale_id"])

        assert sale.was_offline is True
        assert sale.device_sequence == 17
        assert sale.device_id == device.id
        assert abs((sale.device_created_at - rung_up_at).total_seconds()) < 1

    def test_reporting_uses_the_server_clock_not_the_tills(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """A till with a wrong clock must not move revenue between days."""
        device, _token = device_a
        long_ago = timezone.now() - timezone.timedelta(days=30)
        response = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a, device_created_at=long_ago.isoformat())]),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=response.json()["results"][0]["sale_id"])

        assert (timezone.now() - sale.server_received_at).total_seconds() < 60
        assert sale.device_created_at < sale.server_received_at

    def test_stock_moves_when_a_backlog_is_replayed(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a), offline_sale(item_a)]),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            stock = StockItem.objects.get(pk=stock_a.pk)

        assert stock.quantity == 38

    def test_a_batch_may_carry_several_sales(
        self, client_cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a) for _ in range(5)]),
            format="json",
        )

        assert response.json()["accepted"] == 5
        assert len({r["sale_id"] for r in response.json()["results"]}) == 5

    def test_an_empty_batch_is_refused_rather_than_silently_accepted(
        self, client_cashier_a, device_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(SYNC, batch(device, []), format="json")
        assert response.status_code == 400

    def test_a_batch_larger_than_the_cap_is_refused(
        self, client_cashier_a, device_a, item_a, stock_a
    ):
        """One request writes the whole batch inside one transaction, so an
        unbounded batch would hold that transaction open for as long as it
        took. A till with a bigger backlog sends several requests."""
        from apps.sync.serializers import MAX_SALES_PER_BATCH

        device, _token = device_a
        too_many = [offline_sale(item_a) for _ in range(MAX_SALES_PER_BATCH + 1)]

        response = client_cashier_a.post(SYNC, batch(device, too_many), format="json")

        assert response.status_code == 400

    def test_syncing_needs_authentication(self, anon_client, device_a, item_a):
        device, _token = device_a
        response = anon_client.post(SYNC, batch(device, [offline_sale(item_a)]), format="json")
        assert response.status_code == 401


@pytest.mark.django_db
class TestReplayingTheSameSaleTwice:
    """The connection that came back is the one that drops again mid-upload."""

    def test_the_second_arrival_creates_nothing(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        payload = offline_sale(item_a)

        first = client_cashier_a.post(SYNC, batch(device, [payload]), format="json").json()
        second = client_cashier_a.post(SYNC, batch(device, [payload]), format="json").json()

        assert first["results"][0]["status"] == "accepted"
        assert second["results"][0]["status"] == "duplicate"
        assert second["results"][0]["sale_id"] == first["results"][0]["sale_id"]

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 1

    def test_a_replay_is_a_success_not_an_error(
        self, client_cashier_a, device_a, item_a, stock_a
    ):
        """The till has to be able to drop the row from its outbox."""
        device, _token = device_a
        payload = offline_sale(item_a)

        client_cashier_a.post(SYNC, batch(device, [payload]), format="json")
        second = client_cashier_a.post(SYNC, batch(device, [payload]), format="json")

        assert second.status_code == 200
        assert second.json()["rejected"] == 0
        assert second.json()["duplicate"] == 1

    def test_a_replay_does_not_move_stock_again(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        payload = offline_sale(item_a)

        client_cashier_a.post(SYNC, batch(device, [payload]), format="json")
        client_cashier_a.post(SYNC, batch(device, [payload]), format="json")

        with tenant_context(cashier_a.tenant_id):
            assert StockItem.objects.get(pk=stock_a.pk).quantity == 39

    def test_a_replay_does_not_take_the_money_again(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        payload = offline_sale(item_a)

        first = client_cashier_a.post(SYNC, batch(device, [payload]), format="json").json()
        client_cashier_a.post(SYNC, batch(device, [payload]), format="json")

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=first["results"][0]["sale_id"])
            assert sale.payments.count() == 1

    def test_a_replay_does_not_take_a_second_receipt_number(
        self, client_cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        payload = offline_sale(item_a)

        first = client_cashier_a.post(SYNC, batch(device, [payload]), format="json").json()
        second = client_cashier_a.post(SYNC, batch(device, [payload]), format="json").json()

        assert second["results"][0]["receipt_number"] == first["results"][0]["receipt_number"]

    def test_a_duplicate_inside_one_batch_is_caught(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """A till that queued the same sale twice must not sell it twice."""
        device, _token = device_a
        payload = offline_sale(item_a)

        response = client_cashier_a.post(
            SYNC, batch(device, [payload, payload]), format="json"
        ).json()

        assert response["accepted"] == 1
        assert response["duplicate"] == 1
        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 1


@pytest.mark.django_db
class TestTheDeviceMustBelongToThisBusiness:
    def test_an_unregistered_till_is_refused(self, client_cashier_a, item_a, stock_a):
        response = client_cashier_a.post(
            SYNC,
            {
                "device_id": str(uuid.uuid4()),
                "sales": [offline_sale(item_a)],
                "refused_authorizations": [],
            },
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unknown_device"

    def test_a_revoked_till_can_no_longer_sync(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """Revoking a lost tablet has to actually stop it."""
        device, _token = device_a
        with tenant_context(cashier_a.tenant_id):
            Device.objects.filter(pk=device.pk).update(is_active=False)

        response = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a)]), format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unknown_device"

    def test_a_refused_batch_writes_no_sales(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        client_cashier_a.post(
            SYNC,
            {
                "device_id": str(uuid.uuid4()),
                "sales": [offline_sale(item_a)],
                "refused_authorizations": [],
            },
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 0

    def test_a_refused_batch_is_recorded_for_a_person_to_see(
        self, client_cashier_a, cashier_a, item_a, stock_a
    ):
        client_cashier_a.post(
            SYNC,
            {
                "device_id": str(uuid.uuid4()),
                "sales": [offline_sale(item_a)],
                "refused_authorizations": [],
            },
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.UNKNOWN_DEVICE
            )
            assert discrepancy.context["acting_user"] == "mary"
            assert discrepancy.sale_id is None


@pytest.mark.django_db
class TestTheOfflinePinVersionFingerprint:
    """What the version comparison catches, and what it does not.

    It catches a till approving against a PIN that has since been changed or
    revoked. It does not prove the device performed the check - see
    ``check_pin_version`` for why that is not achievable on a rooted tablet.
    """

    def _discounted(self, item, manager, version, **kw):
        return offline_sale(
            item,
            tendered=16200,
            cart_discount_bps=1000,
            discount_authorization={
                "username": manager.username,
                "reason": "Damaged packaging",
                "pin_version": version,
                "authorized_at": timezone.now().isoformat(),
            },
            **kw,
        )

    def test_a_current_version_is_accepted_and_recorded_as_offline(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, manager_with_pin
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC,
            batch(
                device,
                [self._discounted(item_a, manager_with_pin, manager_with_pin.pin_version)],
            ),
            format="json",
        )

        result = response.json()["results"][0]
        assert result["status"] == "accepted"
        assert result["flags"] == []

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])

        assert sale.discount_authorized_via == AuthorizationMethod.OFFLINE
        assert sale.discount_authorization_is_stale is False
        assert sale.discount_authorized_pin_version == manager_with_pin.pin_version
        assert sale.discount_authorized_by_id == manager_with_pin.id

    def test_a_pin_changed_while_the_till_was_offline_is_caught(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, manager_with_pin
    ):
        cached_version = manager_with_pin.pin_version

        with tenant_context(cashier_a.tenant_id):
            manager = User.objects.get(pk=manager_with_pin.pk)
            manager.set_pin("9988")
            manager.save()

        device, _token = device_a
        response = client_cashier_a.post(
            SYNC,
            batch(device, [self._discounted(item_a, manager_with_pin, cached_version)]),
            format="json",
        )

        result = response.json()["results"][0]
        assert result["status"] == "accepted"
        assert "stale_authorization" in result["flags"]

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.STALE_AUTHORIZATION
            )

        assert sale.discount_authorization_is_stale is True
        assert discrepancy.context["problem"] == "stale_pin_version"
        assert discrepancy.context["claimed_pin_version"] == cached_version
        assert discrepancy.context["current_pin_version"] == cached_version + 1

    def test_a_pin_revoked_while_the_till_was_offline_is_caught(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, manager_with_pin
    ):
        """Someone let go while the till was offline must not still approve."""
        cached_version = manager_with_pin.pin_version

        with tenant_context(cashier_a.tenant_id):
            manager = User.objects.get(pk=manager_with_pin.pk)
            manager.clear_pin()
            manager.save()

        device, _token = device_a
        response = client_cashier_a.post(
            SYNC,
            batch(device, [self._discounted(item_a, manager_with_pin, cached_version)]),
            format="json",
        )

        assert "stale_authorization" in response.json()["results"][0]["flags"]

    def test_a_deactivated_manager_is_caught(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, manager_with_pin
    ):
        with tenant_context(cashier_a.tenant_id):
            User.objects.filter(pk=manager_with_pin.pk).update(is_active=False)

        device, _token = device_a
        client_cashier_a.post(
            SYNC,
            batch(
                device,
                [self._discounted(item_a, manager_with_pin, manager_with_pin.pin_version)],
            ),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.STALE_AUTHORIZATION
            )
        assert discrepancy.context["problem"] == "inactive_user"

    def test_a_cashier_named_as_the_authoriser_is_caught(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """The role is checked again on the server, not taken from the till."""
        device, _token = device_a
        client_cashier_a.post(
            SYNC,
            batch(device, [self._discounted(item_a, cashier_a, cashier_a.pin_version)]),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.STALE_AUTHORIZATION
            )
        assert discrepancy.context["problem"] == "insufficient_role"

    def test_an_authoriser_who_does_not_exist_is_caught(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        payload = offline_sale(
            item_a,
            tendered=16200,
            cart_discount_bps=1000,
            discount_authorization={
                "username": "nobody",
                "reason": "Damaged packaging",
                "pin_version": 3,
                "authorized_at": timezone.now().isoformat(),
            },
        )
        response = client_cashier_a.post(SYNC, batch(device, [payload]), format="json")

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=response.json()["results"][0]["sale_id"])
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.STALE_AUTHORIZATION
            )

        assert discrepancy.context["problem"] == "unknown_user"
        # No foreign key, because nobody proved they were that person - but the
        # name they typed is kept, which is the part worth reading.
        assert sale.discount_authorized_by_id is None
        assert sale.discount_authorized_label == "nobody"

    def test_a_stale_authorisation_does_not_reject_the_sale(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, manager_with_pin
    ):
        """The goods already left the shop. Refusing the record deletes the
        evidence, not the problem."""
        cached_version = manager_with_pin.pin_version
        with tenant_context(cashier_a.tenant_id):
            manager = User.objects.get(pk=manager_with_pin.pk)
            manager.set_pin("9988")
            manager.save()

        device, _token = device_a
        response = client_cashier_a.post(
            SYNC,
            batch(device, [self._discounted(item_a, manager_with_pin, cached_version)]),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=response.json()["results"][0]["sale_id"])

        assert sale.state == SaleState.PAID
        assert sale.discount_cents > 0

    def test_a_stale_authorisation_is_written_to_the_audit_trail(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a, manager_with_pin
    ):
        cached_version = manager_with_pin.pin_version
        with tenant_context(cashier_a.tenant_id):
            manager = User.objects.get(pk=manager_with_pin.pk)
            manager.set_pin("9988")
            manager.save()

        device, _token = device_a
        client_cashier_a.post(
            SYNC,
            batch(device, [self._discounted(item_a, manager_with_pin, cached_version)]),
            format="json",
        )

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).first()

        assert entry is not None
        assert entry.reason == "stale_pin_version"

    def test_the_pin_version_carries_nothing_derived_from_the_pin(
        self, tenant_a, manager_with_pin
    ):
        """It is a counter. Two managers with the same PIN share no value, and
        the same manager keeping their PIN does not change it."""
        with tenant_context(tenant_a.id):
            manager = User.objects.get(pk=manager_with_pin.pk)
            before = manager.pin_version
            manager.save()
            manager.refresh_from_db()
            assert manager.pin_version == before

            manager.set_pin("4455")  # the same PIN as before
            assert manager.pin_version == before + 1


@pytest.mark.django_db
class TestWhatTheTillClaimedItCameTo:
    def test_a_matching_total_raises_nothing(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a, total_cents=18000)]), format="json"
        )

        assert response.json()["results"][0]["flags"] == []
        with tenant_context(cashier_a.tenant_id):
            assert SaleDiscrepancy.objects.count() == 0

    def test_a_disagreement_is_recorded_and_the_server_figure_stands(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """Usually a till carrying a price list from before the last change."""
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a, total_cents=15000)]), format="json"
        )

        result = response.json()["results"][0]
        assert "totals_mismatch" in result["flags"]

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.TOTALS_MISMATCH
            )

        assert sale.total_cents == 18000
        assert discrepancy.context["device_total_cents"] == 15000
        assert discrepancy.context["server_total_cents"] == 18000
        assert discrepancy.context["difference_cents"] == -3000

    def test_a_disagreement_does_not_reject_the_sale(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a, total_cents=1)]), format="json"
        )

        assert response.json()["results"][0]["status"] == "accepted"


@pytest.mark.django_db
class TestATillThatUndercharged:
    """The likeliest offline failure there is: a price goes up while a till is
    disconnected, so the cash that came in is less than the sale comes to.

    A live sale is right to refuse that - the cashier is holding too few notes
    and the customer is still standing there. A synced sale is finished. The
    goods are gone and the money is in the drawer, so refusing it would leave
    the books without cash the shop physically has.
    """

    def _raise_price_then_sync(self, client, cashier, device, item, *, tendered=18000):
        with tenant_context(cashier.tenant_id):
            item.price_cents = 20000
            item.save()

        return client.post(
            SYNC,
            batch(device, [offline_sale(item, tendered=tendered, total_cents=18000)]),
            format="json",
        ).json()["results"][0]

    def test_the_sale_is_accepted_not_rejected(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        result = self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        assert result["status"] == "accepted"
        assert "offline_shortfall" in result["flags"]

    def test_the_sale_settles_paid(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        result = self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])

        assert sale.state == SaleState.PAID
        assert sale.receipt_number is not None

    def test_the_stock_moves_because_the_goods_left(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            assert StockItem.objects.get(pk=stock_a.pk).quantity == 39

    def test_the_shortfall_is_recorded_to_the_cent(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        result = self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])

        assert sale.total_cents == 20000
        assert sale.offline_shortfall_cents == 2000

    def test_a_discrepancy_is_raised_with_the_shortfall_and_its_reason(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            discrepancy = SaleDiscrepancy.objects.get(
                kind=SaleDiscrepancy.Kind.OFFLINE_SHORTFALL
            )

        assert discrepancy.context["shortfall_cents"] == 2000
        assert discrepancy.context["tendered_cents"] == 18000
        assert discrepancy.context["due_cents"] == 20000
        assert discrepancy.context["reason"] == "price_changed_while_offline"
        assert discrepancy.is_open

    def test_the_drawer_reconciles_against_what_was_actually_taken(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """The payment ledger must hold the cash that is really in the drawer,
        not the price the sale should have fetched."""
        device, _token = device_a
        result = self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])
            payments = list(sale.payments.all())

        assert len(payments) == 1
        assert payments[0].amount_cents == 18000
        assert payments[0].tendered_cents == 18000
        assert payments[0].change_cents == 0

    def test_the_sale_is_not_marked_overpaid(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """Writing the shortfall off must not make the reduced total look
        exceeded by the cash that came in."""
        device, _token = device_a
        result = self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.get(pk=result["sale_id"]).is_overpaid is False

    def test_a_sale_that_paid_in_full_records_no_shortfall(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        result = client_cashier_a.post(
            SYNC, batch(device, [offline_sale(item_a)]), format="json"
        ).json()["results"][0]

        assert result["flags"] == []
        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])
            assert sale.offline_shortfall_cents == 0
            assert SaleDiscrepancy.objects.filter(
                kind=SaleDiscrepancy.Kind.OFFLINE_SHORTFALL
            ).count() == 0

    def test_a_till_that_overpaid_is_still_handled_as_overpayment(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """Absorbing shortfalls must not swallow the opposite case."""
        device, _token = device_a
        with tenant_context(cashier_a.tenant_id):
            item_a.price_cents = 16000
            item_a.save()

        result = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a, tendered=18000)]),
            format="json",
        ).json()["results"][0]

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])
            # Read inside the binding: the payment rows are tenant-isolated
            # too, and a lazy relation evaluated outside it returns nothing.
            change = sale.payments.first().change_cents

        assert sale.offline_shortfall_cents == 0
        # Tendered above the total is change handed back, not an overpayment.
        assert change == 2000
        assert sale.is_overpaid is False

    def test_a_shortfall_survives_cash_rounding(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """The shortfall is measured against the rounded figure the till would
        actually have asked for, or take_cash would refuse after all."""
        device, _token = device_a
        with tenant_context(cashier_a.tenant_id):
            item_a.price_cents = 20049
            item_a.save()

        result = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a, tendered=18000)]),
            format="json",
        ).json()["results"][0]

        assert result["status"] == "accepted"
        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])

        assert sale.state == SaleState.PAID
        # 20049 rounds to 20000, so the gap is 2000 rather than 2049.
        assert sale.offline_shortfall_cents == 2000

    def test_a_refund_cannot_return_money_that_never_arrived(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        """The written-off part was never collected, so it cannot be given
        back. Refundable is capped at what actually came in."""
        from apps.sales.services import ledger_position

        device, _token = device_a
        result = self._raise_price_then_sync(client_cashier_a, cashier_a, device, item_a)

        with tenant_context(cashier_a.tenant_id):
            sale = Sale.objects.get(pk=result["sale_id"])
            position = ledger_position(sale)

        assert position.refundable_cents == 18000

@pytest.mark.django_db
class TestOneBadSaleDoesNotStrandTheRest:
    def test_a_sale_naming_an_unknown_item_is_rejected_alone(
        self, client_cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        bad = offline_sale(item_a)
        bad["lines"] = [{"item_id": str(uuid.uuid4()), "quantity": "1"}]

        response = client_cashier_a.post(
            SYNC,
            batch(device, [offline_sale(item_a), bad, offline_sale(item_a)]),
            format="json",
        ).json()

        assert response["accepted"] == 2
        assert response["rejected"] == 1
        assert response["results"][1]["status"] == "rejected"

    def test_a_rejected_sale_leaves_nothing_behind(
        self, client_cashier_a, cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        bad = offline_sale(item_a)
        bad["lines"] = [{"item_id": str(uuid.uuid4()), "quantity": "1"}]

        client_cashier_a.post(SYNC, batch(device, [bad]), format="json")

        with tenant_context(cashier_a.tenant_id):
            assert Sale.objects.count() == 0

    def test_the_till_is_told_which_sale_failed_and_why(
        self, client_cashier_a, device_a, item_a, stock_a
    ):
        device, _token = device_a
        bad = offline_sale(item_a)
        bad["lines"] = [{"item_id": str(uuid.uuid4()), "quantity": "1"}]

        response = client_cashier_a.post(SYNC, batch(device, [bad]), format="json").json()
        result = response["results"][0]

        assert result["client_uuid"] == bad["client_uuid"]
        assert result["code"]
        assert result["detail"]


@pytest.mark.django_db
class TestRefusalsThatHappenedOffline:
    """Otherwise the only record of somebody working through a manager's four
    digits at a disconnected till is on the tablet in their hand."""

    def _refusal(self, username="grace", code="bad_credential"):
        return {
            "username": username,
            "reason_code": code,
            "occurred_at": timezone.now().isoformat(),
        }

    def test_offline_refusals_reach_the_audit_trail(
        self, client_cashier_a, cashier_a, device_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC,
            batch(device, [], [self._refusal(), self._refusal(), self._refusal()]),
            format="json",
        )

        assert response.json()["refusals_recorded"] == 3
        with tenant_context(cashier_a.tenant_id):
            assert AuditLog.objects.filter(action=AuditAction.DISCOUNT_REFUSED).count() == 3

    def test_a_refusal_is_filed_against_the_name_typed_not_the_manager(
        self, client_cashier_a, cashier_a, device_a, manager_with_pin
    ):
        """Nobody proved they were that person. Attaching the manager would
        file someone else's guessing in their history."""
        device, _token = device_a
        client_cashier_a.post(SYNC, batch(device, [], [self._refusal()]), format="json")

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.DISCOUNT_REFUSED)

        assert entry.entity_type == "accounts.User"
        assert entry.entity_id == "grace"
        assert entry.after["attempted_authorizer"] == "grace"

    def test_a_refusal_records_who_was_holding_the_till(
        self, client_cashier_a, cashier_a, device_a
    ):
        device, _token = device_a
        client_cashier_a.post(SYNC, batch(device, [], [self._refusal()]), format="json")

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.DISCOUNT_REFUSED)

        assert entry.actor_id == cashier_a.id
        assert entry.after["acting_cashier"] == "mary"
        assert entry.after["offline"] is True

    def test_an_offline_lockout_syncs_as_its_own_reason(
        self, client_cashier_a, cashier_a, device_a
    ):
        """The till locks out locally; the reason has to survive the trip."""
        device, _token = device_a
        client_cashier_a.post(
            SYNC, batch(device, [], [self._refusal(code="locked_out")]), format="json"
        )

        with tenant_context(cashier_a.tenant_id):
            entry = AuditLog.objects.get(action=AuditAction.DISCOUNT_REFUSED)

        assert entry.reason == "locked_out"

    def test_refusals_may_be_sent_with_no_sales_at_all(
        self, client_cashier_a, device_a
    ):
        device, _token = device_a
        response = client_cashier_a.post(
            SYNC, batch(device, [], [self._refusal()]), format="json"
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 0
