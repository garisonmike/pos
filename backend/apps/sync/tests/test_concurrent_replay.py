"""
Two uploads of the same backlog, at the same time.

This is the test the insert-first design exists for. The obvious shape - look
for an existing sale, create one if there is none - passes every sequential
test in the suite and still sells the same bag of sugar twice, because two
threads both run the SELECT before either runs the INSERT.

``TransactionTestCase`` rather than the usual ``pytest.mark.django_db``: the
normal fixture wraps each test in a transaction that is rolled back, and inside
one transaction the threads cannot see each other's writes at all - so the race
being tested would not exist. This class commits for real and cleans up after
itself, which is slower and is the only way the question can be asked.
"""

from __future__ import annotations

import threading
import uuid

from django.db import connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.constants import UserRole
from apps.accounts.models import Device, User
from apps.catalog.models import Item, TaxRate
from apps.core.tenancy import bypass_rls, tenant_context
from apps.inventory.models import MovementReason, StockItem, apply_movement
from apps.sales.models import Sale
from apps.sales.services import CheckoutError
from apps.stores.models import Store
from apps.sync.services import ACCEPTED, DUPLICATE, replay_sale
from apps.tenants.models import BusinessType, TenantStatus
from apps.tenants.services import provision_tenant


class ConcurrentReplayTests(TransactionTestCase):
    """The database constraint is the arbiter, because it is the only thing
    that is actually atomic."""

    reset_sequences = True

    def setUp(self):
        with transaction.atomic(), bypass_rls():
            self.tenant, self.owner = provision_tenant(
                name="Mama Njeri Duka",
                slug="mama-njeri-race",
                business_type=BusinessType.RETAIL,
                status=TenantStatus.ACTIVE,
                owner_username="owner",
                owner_full_name="Owner",
                owner_password="owner-pass-8812",
            )

        with transaction.atomic(), tenant_context(self.tenant.id):
            self.store = Store.objects.create(
                tenant=self.tenant, name="Main", code="MAIN", is_default=True
            )
            self.cashier = User(
                tenant=self.tenant,
                username="mary",
                full_name="Mary Test",
                role=UserRole.CASHIER,
            )
            self.cashier.set_password("staff-pass-4471")
            self.cashier.save()

            rate = TaxRate.objects.create(
                tenant=self.tenant,
                name="VAT 16%",
                rate_bps=1600,
                is_inclusive=True,
                is_default=True,
            )
            self.item = Item.objects.create(
                tenant=self.tenant,
                sku="SUGAR-1KG",
                name="Sugar 1kg",
                price_cents=18000,
                tax_rate=rate,
            )
            stock = StockItem.objects.create(
                tenant=self.tenant, item=self.item, store=self.store
            )
            apply_movement(
                stock_item=stock,
                delta=40,
                reason=MovementReason.PURCHASE,
                note="Opening",
            )
            self.stock = stock
            self.device, _token = Device.issue(tenant=self.tenant, name="Front counter")

    def _payload(self, client_uuid) -> dict:
        return {
            "client_uuid": str(client_uuid),
            "device_created_at": timezone.now(),
            "lines": [{"item_id": str(self.item.id), "quantity": 1}],
            "tendered_cents": 18000,
            "round_to_shilling": True,
        }

    def _replay_in_thread(self, client_uuid, results, index, barrier):
        """Run one replay, released at the same instant as its twin."""
        try:
            barrier.wait(timeout=10)
            # Its own transaction, because tenant binding is a transaction-local
            # setting - and because the race only exists between transactions
            # that commit. Each thread commits for real, so the second one meets
            # the first one's unique key rather than an empty table.
            with transaction.atomic(), tenant_context(self.tenant.id):
                outcome = replay_sale(
                    tenant=self.tenant,
                    store=self.store,
                    cashier=self.cashier,
                    device=self.device,
                    payload=self._payload(client_uuid),
                )
            results[index] = outcome.status
        except CheckoutError as exc:
            results[index] = f"rejected:{exc.code}"
        except Exception as exc:  # pragma: no cover - reported, never swallowed
            results[index] = f"error:{type(exc).__name__}:{exc}"
        finally:
            # Each thread opens its own connection, and a thread that leaves one
            # behind makes the next test's teardown hang on it.
            connections.close_all()

    def _race(self, thread_count=2):
        client_uuid = uuid.uuid4()
        results = [None] * thread_count
        barrier = threading.Barrier(thread_count)
        threads = [
            threading.Thread(
                target=self._replay_in_thread, args=(client_uuid, results, i, barrier)
            )
            for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def test_two_simultaneous_uploads_create_exactly_one_sale(self):
        results = self._race()

        with transaction.atomic(), tenant_context(self.tenant.id):
            self.assertEqual(Sale.objects.count(), 1, f"outcomes were {results}")

    def test_one_upload_wins_and_the_other_is_told_it_is_a_duplicate(self):
        """Neither is an error. Both tills may empty their outbox."""
        results = self._race()

        self.assertEqual(sorted(results), sorted([ACCEPTED, DUPLICATE]), f"got {results}")

    def test_the_customer_is_only_charged_once(self):
        self._race()

        with transaction.atomic(), tenant_context(self.tenant.id):
            sale = Sale.objects.get()
            self.assertEqual(sale.payments.count(), 1)
            self.assertEqual(
                sum(p.amount_cents for p in sale.payments.all()), sale.total_cents
            )

    def test_the_stock_only_moves_once(self):
        self._race()

        with transaction.atomic(), tenant_context(self.tenant.id):
            self.assertEqual(StockItem.objects.get(pk=self.stock.pk).quantity, 39)

    def test_only_one_receipt_number_is_taken(self):
        self._race()

        with transaction.atomic(), tenant_context(self.tenant.id):
            numbers = list(Sale.objects.values_list("receipt_number", flat=True))
            self.assertEqual(numbers, [1])

    def test_five_simultaneous_uploads_still_create_exactly_one_sale(self):
        """Two threads can pass by luck. Five is a harder coincidence."""
        results = self._race(thread_count=5)

        with transaction.atomic(), tenant_context(self.tenant.id):
            self.assertEqual(Sale.objects.count(), 1, f"outcomes were {results}")
        self.assertEqual(results.count(ACCEPTED), 1, f"got {results}")
        self.assertEqual(results.count(DUPLICATE), 4, f"got {results}")
