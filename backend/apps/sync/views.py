"""
The two endpoints a till uses to catch up.

``POST /api/v1/sync/sales/`` sends the backlog up. ``GET /api/v1/sync/catalog/``
brings the price list and staff down. They are deliberately separate: a till
with a full outbox and a stale catalogue needs both, in that order, and
bundling them would mean a failure in one blocked the other.
"""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.constants import UserRole
from apps.accounts.models import User
from apps.catalog.models import Item
from apps.sales.models import SaleDiscrepancy
from apps.sales.services import CheckoutError
from apps.stores.selection import resolve_store_for
from apps.sync.serializers import (
    CatalogItemSerializer,
    CatalogStaffSerializer,
    SyncBatchSerializer,
)
from apps.sync.services import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    SaleOutcome,
    record_offline_refusals,
    replay_sale,
    resolve_device,
)


class SaleSyncView(APIView):
    """Take a till's backlog of offline sales.

    Always answers 200 when the batch itself is well formed, even when every
    sale in it was rejected. The status code describes whether the *batch* was
    understood; what happened to each sale is in the body, because that is the
    granularity the till has to act on.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Upload sales rung up while offline",
        request=SyncBatchSerializer,
        responses={200: None},
    )
    def post(self, request):
        serializer = SyncBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant = request.user.tenant

        # The till has to belong to this business. It is looked up inside the
        # tenant's own scope, so another business's device id is simply absent
        # rather than compared and rejected - see resolve_device.
        device = resolve_device(tenant=tenant, device_id=data["device_id"])
        if device is None:
            SaleDiscrepancy.objects.create(
                tenant=tenant,
                kind=SaleDiscrepancy.Kind.UNKNOWN_DEVICE,
                detail=(
                    "A sync batch named a till this business does not have. "
                    "Either the till was revoked, or the batch came from "
                    "somewhere it should not have."
                ),
                context={
                    "claimed_device_id": str(data["device_id"]),
                    "acting_user": request.user.username,
                    "sale_count": len(data["sales"]),
                },
            )
            return Response(
                {
                    "detail": "That till is not registered to this business.",
                    "code": "unknown_device",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolved only when there is something to file against a branch. A
        # till sending nothing but refused authorisations has no sale to place,
        # and a business that has not set up a branch yet must still be able to
        # send its audit trail home.
        store = None
        if data["sales"]:
            store, store_error = resolve_store_for(request)
            if store_error is not None:
                return store_error

        outcomes: list[SaleOutcome] = []
        for payload in data["sales"]:
            try:
                outcomes.append(
                    replay_sale(
                        tenant=tenant,
                        store=store,
                        cashier=request.user,
                        device=device,
                        payload=payload,
                        request=request,
                    )
                )
            except CheckoutError as exc:
                # replay_sale rolled its own sale back. The rest of the batch
                # is untouched, which is the whole point of per-sale verdicts.
                outcomes.append(
                    SaleOutcome(
                        client_uuid=str(payload["client_uuid"]),
                        status=REJECTED,
                        detail=exc.detail,
                        code=exc.code,
                    )
                )

        refusals = record_offline_refusals(
            cashier=request.user,
            device=device,
            refusals=data["refused_authorizations"],
            request=request,
        )

        device.touch()

        results = [outcome.as_dict() for outcome in outcomes]
        return Response(
            {
                "server_time": timezone.now(),
                "accepted": sum(1 for r in results if r["status"] == ACCEPTED),
                "duplicate": sum(1 for r in results if r["status"] == DUPLICATE),
                "rejected": sum(1 for r in results if r["status"] == REJECTED),
                "refusals_recorded": refusals,
                "results": results,
            }
        )


class CatalogSyncView(APIView):
    """Send down everything a till needs to work with no connection.

    Incremental by ``updated_at``. A till sends back the ``server_time`` it was
    last given and receives only what changed since - which is what makes a
    daily sync over a phone connection affordable.

    ``server_time`` is the server's clock, never the till's. A till whose clock
    is wrong would otherwise ask for a window that skips changes it never saw.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Download the catalogue and staff list for offline use",
        parameters=[
            OpenApiParameter(
                name="since",
                description=(
                    "ISO timestamp from a previous response's server_time. "
                    "Omit for a full download."
                ),
                required=False,
                type=str,
            )
        ],
        responses={200: None},
    )
    def get(self, request):
        # Read the clock before the queries, not after. Taken afterwards, a row
        # written while the queries ran would fall before the returned
        # timestamp and never be sent again - a price change lost for good.
        server_time = timezone.now()
        since = request.query_params.get("since")

        items = Item.objects.all()
        staff = User.objects.filter(tenant=request.user.tenant)

        if since:
            parsed = _parse_since(since)
            if parsed is None:
                return Response(
                    {"detail": "since must be an ISO timestamp.", "code": "bad_since"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            items = items.filter(updated_at__gt=parsed)
            staff = staff.filter(updated_at__gt=parsed)

        items = (
            items.select_related("tax_rate")
            .prefetch_related("barcodes")
            .order_by("updated_at")
        )
        return Response(
            {
                "server_time": server_time,
                "items": CatalogItemSerializer(
                    [_item_row(item) for item in items], many=True
                ).data,
                "staff": CatalogStaffSerializer(
                    [_staff_row(person) for person in staff.order_by("updated_at")],
                    many=True,
                ).data,
            }
        )


def _parse_since(raw: str):
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(raw)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _item_row(item) -> dict:
    """Flatten an item to what a till needs to price it.

    Inactive items are sent rather than filtered out, carrying ``is_active``.
    A till that never hears about a deletion keeps selling the thing; sending
    the flag is how a withdrawal reaches a disconnected till at all.
    """
    return {
        "id": item.id,
        "name": item.name,
        "sku": item.sku or "",
        "barcodes": [code.code for code in item.barcodes.all()],
        "unit": item.unit,
        "price_cents": item.price_cents,
        "is_price_variable": item.is_price_variable,
        "tax_rate_bps": item.tax_rate.rate_bps if item.tax_rate_id else 0,
        "tax_is_inclusive": item.tax_rate.is_inclusive if item.tax_rate_id else False,
        "is_active": item.is_active,
        "updated_at": item.updated_at,
    }


def _staff_row(person: User) -> dict:
    """Flatten a user to what a till needs to sign them in and check approvals.

    The PIN hash is included only for people who can actually authorise
    something. A cashier's hash would never be consulted by an offline check,
    so downloading it would widen what a stolen tablet gives up for no gain.
    """
    may_authorize = person.has_role_at_least(UserRole.MANAGER)
    return {
        "id": person.id,
        "username": person.username,
        "full_name": person.full_name,
        "role": person.role,
        "pin_hash": person.pin_hash if may_authorize else "",
        "pin_version": person.pin_version,
        "is_active": person.is_active,
        "updated_at": person.updated_at,
    }
