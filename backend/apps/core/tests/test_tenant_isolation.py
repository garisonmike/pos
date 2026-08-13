"""
Behavioural proof that one business cannot reach another's data.

The tests above check that the protections exist. These check that they work,
including in the two cases that would otherwise be easy to get wrong: code that
forgets to filter, and a connection reused between requests from different
businesses.
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction

from apps.accounts.models import User
from apps.catalog.models import Item
from apps.core.tenancy import TENANT_GUC, bypass_rls, tenant_context
from apps.stores.models import Store

pytestmark = pytest.mark.django_db


def test_unfiltered_query_returns_nothing_from_another_tenant(item_a, tenant_b):
    """The central guarantee, stated as plainly as it can be.

    ``all_objects`` is the unfiltered manager - it stands in for application
    code that forgot a filter. Bound to business B, it still returns nothing of
    business A's, because the database refuses to hand the rows over.
    """
    with transaction.atomic(), tenant_context(tenant_b.id):
        assert Item.all_objects.count() == 0
        assert not Item.all_objects.filter(sku="SUGAR-1KG").exists()


def test_an_unbound_request_sees_no_data_at_all(item_a):
    """With no tenant bound, the safe default is emptiness, not everything.

    This is the case that decides whether a bug is an inconvenience or a
    breach: if the policy defaulted the other way, any request that failed to
    bind a tenant would see every business on the platform.
    """
    with transaction.atomic(), tenant_context(None):
        assert Item.all_objects.count() == 0


def test_a_tenant_sees_its_own_rows(item_a, tenant_a):
    """The isolation is not achieved by breaking everything."""
    with transaction.atomic(), tenant_context(tenant_a.id):
        assert Item.all_objects.count() == 1
        assert Item.objects.get(sku="SUGAR-1KG").name == "Sugar 1kg"


def test_writing_a_row_stamped_for_another_tenant_is_refused(tenant_a, tenant_b):
    """WITH CHECK covers writes, not just reads.

    Without it, a business could not read another's data but could still write
    into it, which is arguably worse: silent corruption rather than a visible
    leak.
    """
    from django.db.utils import ProgrammingError

    with transaction.atomic(), tenant_context(tenant_a.id):
        with pytest.raises(ProgrammingError):
            Store.objects.create(
                tenant=tenant_b, name="Smuggled", code="SMUG", is_default=False
            )


def test_bypass_is_required_to_see_across_tenants(item_a, tenant_b):
    """Cross-tenant reads are possible only with isolation explicitly lifted."""
    with transaction.atomic(), bypass_rls():
        assert Item.all_objects.count() == 1


def test_binding_is_restored_after_a_nested_context(tenant_a, tenant_b):
    """Nesting must not leave the wrong business bound behind it.

    The sign-in endpoints rely on this: they bind a business to read its user
    table, and anything after them must not inherit that binding.
    """
    with transaction.atomic(), tenant_context(tenant_a.id):
        assert _bound_tenant() == str(tenant_a.id)
        with tenant_context(tenant_b.id):
            assert _bound_tenant() == str(tenant_b.id)
        assert _bound_tenant() == str(tenant_a.id)


def test_binding_is_cleared_when_the_block_exits(tenant_a):
    """A binding must not outlive the block that set it.

    This is the connection-reuse guarantee at its smallest: the next piece of
    work on this connection starts unbound, so it sees nothing rather than
    inheriting whoever came before.
    """
    with transaction.atomic():
        with tenant_context(tenant_a.id):
            assert _bound_tenant() == str(tenant_a.id)
        assert _bound_tenant() == ""


@pytest.mark.django_db(transaction=True)
def test_binding_does_not_survive_the_transaction(tenant_a):
    """The database-level backstop for connection reuse.

    Even if the context manager's own cleanup were removed, the binding is set
    with ``is_local``, so committing or rolling back discards it. Two layers,
    because a pooled connection carrying a stale tenant would be the single
    worst bug this system could have.
    """
    with transaction.atomic(), tenant_context(tenant_a.id):
        assert _bound_tenant() == str(tenant_a.id)

    assert _bound_tenant() == ""


def test_binding_outside_a_transaction_is_refused(tenant_a):
    """Failing loudly beats binding a tenant that does not take effect.

    Outside a transaction ``is_local`` silently does nothing, so the policies
    would see no tenant and every query would come back empty. A clear
    exception is far easier to diagnose than a system that returns no data for
    reasons nobody can find.

    The absence of a transaction is simulated rather than created by switching
    the connection to autocommit: the test itself runs inside a transaction
    that pytest rolls back, and genuinely leaving it would strand the
    connection in a state every following test inherits.
    """
    from django.db import DEFAULT_DB_ALIAS, connections

    from apps.core.tenancy import TenantBindingError

    wrapper = connections[DEFAULT_DB_ALIAS]
    was_in_atomic_block = wrapper.in_atomic_block
    wrapper.in_atomic_block = False
    try:
        with pytest.raises(TenantBindingError):
            with tenant_context(tenant_a.id):
                pass
        with pytest.raises(TenantBindingError):
            with bypass_rls():
                pass
    finally:
        wrapper.in_atomic_block = was_in_atomic_block


def test_users_of_one_business_are_invisible_to_another(cashier_a, cashier_b, tenant_a):
    """Two businesses can both employ a 'mary' without seeing each other's.

    Per-business username uniqueness only works if the lookup is scoped, so
    this doubles as a check that the scoping holds for the user table - the one
    table where a leak would also be an authentication problem.
    """
    with transaction.atomic(), tenant_context(tenant_a.id):
        marys = User.all_objects.filter(username="mary")
        assert marys.count() == 1
        assert marys.first().tenant_id == tenant_a.id


def _bound_tenant() -> str:
    """Read the tenant currently bound on this connection."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(current_setting(%s, true), '')", [TENANT_GUC])
        return cursor.fetchone()[0]
