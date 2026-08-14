"""
Deciding which branch a sale belongs to.

Lives here rather than on the checkout view because there are now three callers
- cash checkout, M-Pesa checkout and offline sync - and a branch resolved one
way at the till and another way at sync would file the same sale's stock
movement against different shops depending on whether the network was up.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from apps.stores.models import Store


def resolve_store_for(request, store_id=None):
    """Return ``(store, error_response)``; exactly one of them is None.

    Defaults to the business's default branch. With several branches and no
    choice made, refusing beats guessing: a sale filed against the wrong branch
    takes its stock movement with it, and the shop discovers the mistake as a
    stock count that will not reconcile weeks later.
    """
    stores = Store.objects.filter(tenant=request.user.tenant, is_active=True)

    if store_id:
        store = stores.filter(pk=store_id).first()
        if store is None:
            return None, Response(
                {"detail": "No such branch.", "code": "not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return store, None

    if request.user.store_id:
        store = stores.filter(pk=request.user.store_id).first()
        if store is not None:
            return store, None

    default = stores.filter(is_default=True).first()
    if default is not None:
        return default, None

    if stores.count() > 1:
        return None, Response(
            {
                "detail": (
                    "This business has several branches and none is marked "
                    "default. Say which branch this sale belongs to."
                ),
                "code": "store_required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    only = stores.first()
    if only is None:
        return None, Response(
            {"detail": "This business has no active branch.", "code": "no_store"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return only, None
