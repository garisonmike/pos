"""
Structural guarantees about tenant isolation.

These tests do not exercise behaviour; they inspect the database's own
catalogue and assert that the protections are actually in place. That matters
because the failure they guard against is silent: a new model with a
``tenant`` column but no policy behaves perfectly in every functional test,
right up until someone queries it without a filter.

If one of these fails, the fix is almost always to add
``enable_rls("<table>")`` to the migration that created the table.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.apps import apps
from django.db import connection, models, transaction
from django.utils import timezone

from apps.core.db.rls import POLICY_NAME
from apps.core.tenancy import bypass_rls, tenant_context

pytestmark = pytest.mark.django_db


def _concrete_models() -> list[type[models.Model]]:
    """Every real table, including the join tables Django creates for itself.

    ``include_auto_created`` is the important part. The many-to-many join
    tables behind ``PermissionsMixin`` are real tables holding real rows, but
    they are not declared anywhere in this codebase, so a walk over ordinary
    models steps straight past them. That is exactly how they went unprotected
    through milestone 1.
    """
    return [
        model
        for model in apps.get_models(include_auto_created=True)
        if not model._meta.abstract and not model._meta.proxy
    ]


def models_with_tenant_column() -> list[type[models.Model]]:
    """Every concrete model carrying a ``tenant`` column.

    Detected by inspecting fields rather than by checking for a base class,
    because two models - the user and the audit log - carry a nullable tenant
    without inheriting ``TenantOwnedModel``. Checking the column is what the
    database cares about, so it is what this checks.
    """
    return [
        model
        for model in _concrete_models()
        if any(field.name == "tenant" for field in model._meta.local_fields)
    ]


def models_reaching_a_tenant() -> list[type[models.Model]]:
    """Every model that belongs to a business, directly or through a relation.

    A table with no ``tenant_id`` still holds one business's rows if it points
    at something that does. Following those references is what this adds over
    the direct check: simplejwt's outstanding-token table carries no tenant, but
    it stores an encoded refresh token beside the id of the user it was issued
    to, which makes every row in it the property of exactly one business.

    Resolved transitively, so a table pointing at a table pointing at a
    tenant-owned row is caught too.
    """
    owned = set(models_with_tenant_column())
    everything = _concrete_models()

    # Repeat until nothing new is found, so indirection of any depth is caught.
    changed = True
    while changed:
        changed = False
        for model in everything:
            if model in owned:
                continue
            for field in model._meta.local_fields:
                if field.is_relation and field.related_model in owned:
                    owned.add(model)
                    changed = True
                    break

    return sorted(owned, key=lambda m: m._meta.db_table)


def test_there_are_tenant_owned_models():
    """Guards the tests below from passing vacuously on an empty list."""
    assert models_with_tenant_column(), "Expected at least one tenant-owned model."


def test_tables_reaching_a_tenant_indirectly_are_also_protected():
    """The gap that milestone 1's direct-column check walked past.

    Tables with no tenant column of their own, but holding rows that belong to
    one business by way of a foreign key. Left unprotected, the worst of them
    would have let a query made under one business read another's refresh
    tokens verbatim.

    If this fails for a newly added table, protect it with ``enable_rls_via``
    rather than by copying the tenant predicate, so the rule stays defined by
    the parent's visibility.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_policies WHERE policyname = %s", [POLICY_NAME]
        )
        protected = {row[0] for row in cursor.fetchall()}

    missing = sorted(
        model._meta.db_table
        for model in models_reaching_a_tenant()
        if model._meta.db_table not in protected
    )
    assert not missing, (
        "These tables hold rows belonging to a single business - directly or "
        f"through a foreign key - but carry no isolation policy: {missing}. "
        "Protect each with enable_rls() or enable_rls_via() in a migration."
    )


class TestNullableParentReferencesFailClosed:
    """A row whose parent link is null must be invisible, not universally visible.

    ``enable_rls_via`` defines visibility as ``EXISTS (SELECT 1 FROM parent ...)``.
    With a null foreign key that subquery matches nothing, so the row is hidden
    from every business - which is the direction a mistake here has to fall.

    This is not hypothetical for the table used below.
    ``token_blacklist_outstandingtoken.user_id`` is nullable, and the row holds
    an encoded refresh token. When a user is removed the token row can outlive
    them, and an orphaned credential that became visible to *everyone* rather
    than *no one* would be considerably worse than the gap this policy closed.

    The bypass clause is what keeps such a row reachable at all, so the platform
    operator can still find and clear it.
    """

    @staticmethod
    def _orphan_token():
        """An outstanding token with no user, as a deleted account would leave.

        Written under bypass because the policy's WITH CHECK refuses it
        otherwise - which is itself the write-side half of failing closed.
        """
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        with transaction.atomic(), bypass_rls():
            return OutstandingToken.objects.create(
                user=None,
                jti="orphaned-jti-for-test",
                token="fake.refresh.token",
                created_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=1),
            )

    def test_an_orphaned_row_is_invisible_to_the_business_it_came_from(self, tenant_a):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        orphan = self._orphan_token()

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert not OutstandingToken.objects.filter(pk=orphan.pk).exists()

    def test_an_orphaned_row_is_invisible_to_every_other_business(
        self, tenant_a, tenant_b
    ):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        orphan = self._orphan_token()

        for tenant in (tenant_a, tenant_b):
            with transaction.atomic(), tenant_context(tenant.id):
                assert not OutstandingToken.objects.filter(pk=orphan.pk).exists()

    def test_an_orphaned_row_is_invisible_with_no_business_bound(self):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        orphan = self._orphan_token()

        with transaction.atomic(), tenant_context(None):
            assert not OutstandingToken.objects.filter(pk=orphan.pk).exists()

    def test_an_orphaned_row_is_still_visible_to_the_platform(self):
        """Hidden from every business, but not lost.

        Without the bypass clause in the policy the row would be unreachable by
        any query at all, leaving a stored credential that nobody could find to
        revoke.
        """
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        orphan = self._orphan_token()

        with transaction.atomic(), bypass_rls():
            assert OutstandingToken.objects.filter(pk=orphan.pk).exists()

    def test_a_row_with_a_parent_is_visible_only_to_that_parents_business(
        self, tenant_a, tenant_b, cashier_a
    ):
        """The ordinary case, alongside the null case, so the pair reads together."""
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        with transaction.atomic(), tenant_context(tenant_a.id):
            owned = OutstandingToken.objects.create(
                user=cashier_a,
                jti="owned-jti-for-test",
                token="fake.refresh.token",
                created_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=1),
            )

        with transaction.atomic(), tenant_context(tenant_a.id):
            assert OutstandingToken.objects.filter(pk=owned.pk).exists()

        with transaction.atomic(), tenant_context(tenant_b.id):
            assert not OutstandingToken.objects.filter(pk=owned.pk).exists()


def test_every_tenant_table_has_an_isolation_policy():
    """A tenant column without a policy is a table that leaks."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_policies WHERE policyname = %s", [POLICY_NAME]
        )
        protected = {row[0] for row in cursor.fetchall()}

    missing = sorted(
        model._meta.db_table
        for model in models_with_tenant_column()
        if model._meta.db_table not in protected
    )
    assert not missing, (
        "These tables carry a tenant but have no Row-Level Security policy, so "
        f"a missing queryset filter would expose other businesses' rows: {missing}. "
        "Add enable_rls('<table>') to the migration that creates each one."
    )


def test_every_tenant_table_forces_row_level_security():
    """FORCE is what makes the policy apply to the table's owner.

    Django runs migrations as the same role the application uses, so that role
    owns every table. Plain ENABLE exempts the owner, which would leave all the
    policies in place and completely inert. This is the difference between a
    system that is isolated and one that only looks it.
    """
    tables = [model._meta.db_table for model in models_reaching_a_tenant()]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE relname = ANY(%s)
            """,
            [tables],
        )
        rows = {name: (enabled, forced) for name, enabled, forced in cursor.fetchall()}

    not_enabled = sorted(t for t in tables if not rows.get(t, (False, False))[0])
    not_forced = sorted(t for t in tables if not rows.get(t, (False, False))[1])

    assert not not_enabled, f"Row-Level Security is not enabled on: {not_enabled}"
    assert not not_forced, (
        f"Row-Level Security is not FORCEd on: {not_forced}. Without FORCE the "
        "policies are ignored for the table owner, which is the role this "
        "application connects as."
    )


def test_application_role_cannot_bypass_row_level_security():
    """The connected role must be subject to the policies it is protected by.

    A superuser, or any role with BYPASSRLS, ignores every policy in this
    system. Connecting as one would make the entire isolation strategy
    decorative, and would do so invisibly - every test above would still pass.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        is_superuser, can_bypass = cursor.fetchone()

    assert not is_superuser, (
        "The application is connected to Postgres as a superuser, which "
        "bypasses every Row-Level Security policy. Check DB_USER in .env."
    )
    assert not can_bypass, (
        "The application's database role has BYPASSRLS, which defeats tenant "
        "isolation entirely."
    )


def test_no_model_stores_money_in_a_float():
    """Money must never be a float, anywhere.

    A float cannot represent 0.10 exactly, so totals drift by fractions of a
    cent that accumulate into a till that will not balance. Amounts are stored
    as an integer number of cents; see ``apps.core.money``.

    This checks every FloatField rather than only suspiciously named ones,
    because there is no legitimate use for a float in this schema yet.
    """
    offenders = []
    for model in apps.get_models():
        if model._meta.abstract:
            continue
        for field in model._meta.local_fields:
            if isinstance(field, models.FloatField):
                offenders.append(f"{model._meta.label}.{field.name}")

    assert not offenders, (
        f"Float fields found: {offenders}. Money belongs in a BigIntegerField "
        "of cents, and quantities in a DecimalField."
    )
