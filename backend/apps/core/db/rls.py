"""
Row-Level Security policy helpers used from migrations.

Every table that carries a ``tenant_id`` gets the same policy, generated here
rather than hand-written per migration, so there is one definition to review
and no opportunity for a table to receive a subtly weaker rule.

The policy reads:

    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    OR COALESCE(current_setting('app.bypass_rls', true), '') = 'on'

Three details are doing real work:

``NULLIF(..., '')``
    An unset variable yields an empty string. Casting that to uuid would raise,
    turning a missing binding into a 500. Mapping it to NULL instead makes the
    comparison false, so an unbound request sees nothing. The failure mode is
    "no data", never "someone else's data".

``current_setting(..., true)``
    The ``true`` is ``missing_ok``. Without it, a connection that has never had
    the variable set raises rather than returning NULL.

``FORCE ROW LEVEL SECURITY``
    Ordinary ``ENABLE`` exempts the table owner, and the application role owns
    these tables because it runs the migrations. Without FORCE, every policy
    here would be inert in production. This is the easiest way to build a
    system that looks isolated and is not.
"""

from __future__ import annotations

from django.db import migrations

POLICY_NAME = "tenant_isolation"

_PREDICATE = (
    "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid "
    "OR COALESCE(current_setting('app.bypass_rls', true), '') = 'on'"
)


def enable_rls(table: str) -> migrations.RunSQL:
    """Return the operation that puts the standard isolation policy on a table.

    ``WITH CHECK`` mirrors ``USING`` so that isolation covers writes as well as
    reads: a request bound to tenant A cannot insert or update a row stamped
    with tenant B, even if application code tried to.
    """
    return migrations.RunSQL(
        sql=[
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
            f"""
            CREATE POLICY {POLICY_NAME} ON {table}
                USING ({_PREDICATE})
                WITH CHECK ({_PREDICATE});
            """,
        ],
        reverse_sql=[
            f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table};",
            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;",
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;",
        ],
    )


def enable_rls_for(*tables: str) -> list[migrations.RunSQL]:
    """Convenience wrapper for migrations that add several tenant tables."""
    return [enable_rls(table) for table in tables]


def enable_rls_via(
    table: str, column: str, parent_table: str, parent_column: str = "id"
) -> migrations.RunSQL:
    """Protect a table that belongs to a business only indirectly.

    Some tables carry no ``tenant_id`` of their own but still hold rows that
    belong to exactly one business, because they point at something that does.
    Django's own tables are the main source of these: the many-to-many join
    tables behind ``PermissionsMixin``, the admin log, and simplejwt's
    outstanding-token table, which stores the encoded refresh token itself
    alongside the user it was issued to.

    Visibility is defined by the parent's visibility::

        EXISTS (SELECT 1 FROM accounts_user p WHERE p.id = <table>.user_id)

    The subquery is evaluated as the querying role, so the parent's own policy
    applies inside it. A row whose parent is invisible is therefore invisible
    too, and the rule stays correct automatically as the parent's policy
    changes - there is no second copy of the tenant logic to keep in step.

    The bypass clause is repeated here rather than relied upon through the
    parent, so that a row whose foreign key is null is still reachable from the
    platform surface instead of being orphaned beyond any query's reach.
    """
    predicate = (
        f"EXISTS (SELECT 1 FROM {parent_table} rls_parent "
        f"WHERE rls_parent.{parent_column} = {table}.{column}) "
        "OR COALESCE(current_setting('app.bypass_rls', true), '') = 'on'"
    )
    return migrations.RunSQL(
        sql=[
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
            f"""
            CREATE POLICY {POLICY_NAME} ON {table}
                USING ({predicate})
                WITH CHECK ({predicate});
            """,
        ],
        reverse_sql=[
            f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table};",
            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;",
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;",
        ],
    )
