"""
Request-scoped tenant binding.

This module is the single place where the current tenant is established, and
everything else in the system reads from it. There are two co-operating pieces
of state, and they exist at different layers on purpose:

1. A ``ContextVar`` holding the current tenant id. The ORM layer reads this so
   that querysets are scoped without every call site remembering to filter.

2. A Postgres session variable (``app.tenant_id``) set with ``set_config(...,
   is_local => true)``. Row-Level Security policies read this. It is the layer
   that actually makes a cross-tenant leak impossible rather than merely
   unlikely, because it is enforced by the database and cannot be forgotten by
   application code.

The ``is_local`` flag is what keeps the binding from outliving the request. A
non-local ``SET`` would persist on the connection, and with connection pooling
the next request - possibly another tenant's - would inherit it. That is the
single most dangerous mistake available in this design, so the helpers here
refuse to run outside a transaction, where ``is_local`` would silently do
nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import DatabaseError, connection

#: Postgres session variable read by every tenant isolation policy.
TENANT_GUC = "app.tenant_id"

#: Postgres session variable that lifts isolation for platform-level work.
BYPASS_GUC = "app.bypass_rls"

_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_tenant_id", default=None
)
_bypass_active: ContextVar[bool] = ContextVar("bypass_rls_active", default=False)


class TenantBindingError(RuntimeError):
    """Raised when a tenant binding is attempted outside a transaction.

    Outside a transaction ``set_config(..., true)`` has no effect, so the RLS
    policies would fall back to "no tenant set" and silently return nothing.
    Failing loudly is far better than a system that appears to work but returns
    empty results, or worse, one that leaks because a later change flipped the
    default the other way.
    """


def get_current_tenant_id() -> uuid.UUID | None:
    """Return the tenant bound to the current execution context, if any."""
    return _current_tenant_id.get()


def is_bypass_active() -> bool:
    """Whether Row-Level Security is currently lifted for platform-level work."""
    return _bypass_active.get()


def _require_transaction() -> None:
    if not connection.in_atomic_block:
        raise TenantBindingError(
            "Tenant binding requires an open transaction. Wrap the call in "
            "django.db.transaction.atomic(); requests get this from "
            "TenantBindingMiddleware."
        )


def _read_guc(name: str) -> str:
    """Read a session variable, tolerating it never having been set."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(current_setting(%s, true), '')", [name])
        return cursor.fetchone()[0]


def _write_guc(name: str, value: str) -> None:
    """Set a session variable for the duration of the current transaction.

    ``set_config`` is used rather than ``SET LOCAL`` because Postgres does not
    accept bind parameters in a ``SET`` statement, and building that statement
    by string interpolation would put a request-controlled value into SQL.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [name, value])


def _restore_guc(name: str, value: str) -> None:
    """Put a session variable back, tolerating an already-failed transaction.

    If a query inside the block raised, the transaction is in an aborted state
    and any further statement on it - including this restore - would raise too.
    That is harmless to skip: the setting was made with ``is_local``, so the
    rollback that must follow discards it regardless.

    Skipping quietly matters because the alternative is worse than untidy. An
    exception thrown from cleanup would replace the real error with a confusing
    one, and would abandon the restore of the in-process binding.
    """
    if not connection.in_atomic_block or connection.needs_rollback:
        return
    try:
        _write_guc(name, value)
    except DatabaseError:
        # The transaction failed between the check above and the write. The
        # rollback still discards the setting, so there is nothing to repair.
        pass


@contextmanager
def tenant_context(tenant_id: uuid.UUID | str | None) -> Iterator[None]:
    """Bind a tenant for the duration of the block.

    Restores whatever binding was in place on exit, so nesting behaves the way
    a reader would expect - for example a platform task looping over tenants,
    or a login view that resolves a tenant from a slug before it can read the
    user table.
    """
    _require_transaction()

    normalised = str(tenant_id) if tenant_id is not None else ""
    previous_guc = _read_guc(TENANT_GUC)
    token = _current_tenant_id.set(
        uuid.UUID(normalised) if normalised else None
    )
    _write_guc(TENANT_GUC, normalised)
    try:
        yield
    finally:
        # The in-process binding is reset first and unconditionally. If the
        # database restore below is skipped because the transaction has already
        # failed, the context variable must still be put back - otherwise the
        # next piece of work in this process inherits a tenant that may not
        # even exist any more, and writes rows pointing at it.
        _current_tenant_id.reset(token)
        _restore_guc(TENANT_GUC, previous_guc)


@contextmanager
def bypass_rls() -> Iterator[None]:
    """Lift tenant isolation for genuinely cross-tenant work.

    Used by exactly two things: the platform administration surfaces, and
    bootstrap commands that create platform administrators before any tenant
    exists. Both are gated on the caller being a platform administrator; see
    ``apps.core.middleware.TenantBindingMiddleware`` for where that gate is.

    Every use of this is a deliberate hole in the isolation guarantee, so it is
    kept small, named plainly, and never reachable from a tenant-facing route.
    """
    _require_transaction()

    previous_guc = _read_guc(BYPASS_GUC)
    token = _bypass_active.set(True)
    _write_guc(BYPASS_GUC, "on")
    try:
        yield
    finally:
        _bypass_active.reset(token)
        _restore_guc(BYPASS_GUC, previous_guc)
