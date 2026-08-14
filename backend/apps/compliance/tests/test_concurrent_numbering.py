"""
Two tills invoicing at the same instant.

Gaplessness is the whole property of this series, and it cannot be tested
sequentially. Two cashiers settling at the same moment either queue on the
counter row or both read the same last number and issue it twice, and only a
real race tells you which.

``TransactionTestCase`` for the same reason the sync replay test uses it: the
usual per-test transaction is rolled back and never commits, so threads cannot
see each other's writes and the race being tested would not exist.
"""

from __future__ import annotations

import threading

from django.db import connections, transaction
from django.test import TransactionTestCase

from apps.compliance.models import InvoiceCounter
from apps.compliance.numbering import allocate_invoice_number
from apps.core.tenancy import bypass_rls, tenant_context
from apps.tenants.models import BusinessType, TenantStatus
from apps.tenants.services import provision_tenant


class ConcurrentInvoiceNumberingTests(TransactionTestCase):
    """The counter row is the arbiter, because the lock on it is the only thing
    that is actually atomic."""

    reset_sequences = True

    def setUp(self):
        with transaction.atomic(), bypass_rls():
            self.tenant, self.owner = provision_tenant(
                name="Mama Njeri Duka",
                slug="mama-njeri-invoices",
                business_type=BusinessType.RETAIL,
                status=TenantStatus.ACTIVE,
                owner_username="owner",
                owner_full_name="Owner",
                owner_password="owner-pass-8812",
            )
            self.other, _owner = provision_tenant(
                name="Kwa Baba Hardware",
                slug="kwa-baba-invoices",
                business_type=BusinessType.RETAIL,
                status=TenantStatus.ACTIVE,
                owner_username="owner",
                owner_full_name="Owner",
                owner_password="owner-pass-8812",
            )

    def _allocate(self, tenant, results, index, barrier):
        """Take one number, released at the same instant as its twins."""
        try:
            barrier.wait(timeout=10)
            with transaction.atomic(), tenant_context(tenant.id):
                number, _code = allocate_invoice_number(tenant)
            results[index] = number
        except Exception as exc:  # pragma: no cover - reported, never swallowed
            results[index] = f"error:{type(exc).__name__}:{exc}"
        finally:
            # A thread that leaves a connection behind makes the next test's
            # teardown hang on it.
            connections.close_all()

    def _race(self, tenant, thread_count=5):
        results = [None] * thread_count
        barrier = threading.Barrier(thread_count)
        threads = [
            threading.Thread(target=self._allocate, args=(tenant, results, i, barrier))
            for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def test_five_tills_take_five_different_numbers(self):
        results = self._race(self.tenant)

        self.assertEqual(len(set(results)), 5, f"got {results}")

    def test_the_series_has_no_gaps(self):
        """The property a revenue authority actually looks at. A gap invites
        the question of what was removed from it."""
        results = self._race(self.tenant)

        self.assertEqual(sorted(results), [1, 2, 3, 4, 5], f"got {results}")

    def test_no_thread_errored(self):
        results = self._race(self.tenant)

        for outcome in results:
            self.assertIsInstance(outcome, int, f"got {results}")

    def test_the_counter_agrees_with_what_was_handed_out(self):
        self._race(self.tenant)

        with transaction.atomic(), tenant_context(self.tenant.id):
            counter = InvoiceCounter.objects.get()

        self.assertEqual(counter.last_number, 5)

    def test_ten_at_once_still_has_no_gaps(self):
        """Five can pass by luck on a fast machine. Ten is a harder
        coincidence."""
        results = self._race(self.tenant, thread_count=10)

        self.assertEqual(sorted(results), list(range(1, 11)), f"got {results}")

    def test_two_businesses_racing_do_not_share_a_series(self):
        """One shop's traffic must not advance another's tax numbering."""
        results = [None] * 6
        barrier = threading.Barrier(6)
        threads = [
            threading.Thread(
                target=self._allocate,
                args=(self.tenant if i % 2 == 0 else self.other, results, i, barrier),
            )
            for i in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        ours = sorted(results[i] for i in range(6) if i % 2 == 0)
        theirs = sorted(results[i] for i in range(6) if i % 2 == 1)

        self.assertEqual(ours, [1, 2, 3], f"got {results}")
        self.assertEqual(theirs, [1, 2, 3], f"got {results}")
