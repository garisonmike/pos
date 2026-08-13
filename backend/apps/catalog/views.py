"""
Catalogue endpoints.

Everyone in a shop reads these all day; only managers and owners change them.
That split is the ``ReadOnlyOrManager`` permission, and it matters more here
than almost anywhere else: a cashier needs to look items up constantly and must
never be able to edit a price mid-shift.

Every queryset is filtered by the caller's business explicitly, on top of the
manager's scoping and the database policy. Three layers is deliberate - the
explicit filter is what makes the intent obvious to someone reading one view in
isolation.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from apps.catalog.imports import (
    ImportError_,
    build_report,
    build_template_csv,
    check_against_catalogue,
    commit_rows,
    consume_token,
    issue_token,
    parse_rows,
    read_csv,
)
from apps.catalog.models import Barcode, Category, Item, TaxRate
from apps.catalog.serializers import (
    BarcodeSerializer,
    CategorySerializer,
    ItemSerializer,
    ItemWriteSerializer,
    TaxRateSerializer,
)
from apps.core.audit import diff_fields, record_audit
from apps.core.models import AuditAction
from apps.core.permissions import IsManagerOrAbove, ReadOnlyOrManager


@extend_schema(tags=["catalog"])
class CategoryViewSet(viewsets.ModelViewSet):
    """Groupings used for navigation at the till and for reporting.

    Deactivated rather than deleted once items reference them, because deleting
    a category that a year of sales reports group by would silently change what
    those reports say.
    """

    serializer_class = CategorySerializer
    permission_classes = [ReadOnlyOrManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name"]
    ordering_fields = ["display_order", "name", "created_at"]
    ordering = ["display_order", "name"]

    def get_queryset(self):
        """Categories of the caller's business, with a live item count."""
        if getattr(self, "swagger_fake_view", False):
            return Category.objects.none()
        return (
            Category.objects.filter(tenant=self.request.user.tenant)
            .select_related("parent")
            .annotate(item_count=Count("items", distinct=True))
        )

    def perform_create(self, serializer):
        category = serializer.save(tenant=self.request.user.tenant)
        record_audit(
            action=AuditAction.CREATE,
            entity=category,
            actor=self.request.user,
            request=self.request,
            after={"name": category.name, "parent": category.parent_id},
        )

    def perform_update(self, serializer):
        before = diff_fields(serializer.instance, ["name", "parent", "is_active"])
        category = serializer.save()
        record_audit(
            action=AuditAction.UPDATE,
            entity=category,
            actor=self.request.user,
            request=self.request,
            before=before,
            after=diff_fields(category, ["name", "parent", "is_active"]),
        )

    def perform_destroy(self, instance):
        """Refuse to delete a category that still has items.

        ``PROTECT`` on the foreign key would raise a database error here. This
        turns it into an answer the person can act on.
        """
        if instance.items.exists():
            raise ValidationError(
                {
                    "detail": (
                        "This category still has items in it. Move them first, "
                        "or deactivate the category instead of deleting it."
                    )
                }
            )
        record_audit(
            action=AuditAction.DELETE,
            entity=instance,
            actor=self.request.user,
            request=self.request,
            before={"name": instance.name},
        )
        instance.delete()


@extend_schema(tags=["catalog"])
class TaxRateViewSet(viewsets.ModelViewSet):
    """Tax rates, and whether prices carrying them already include the tax.

    Rates are never deleted once anything references them: a sale from last year
    recorded VAT at the rate in force then, and removing the rate would leave
    that sale unable to explain its own figures.
    """

    serializer_class = TaxRateSerializer
    permission_classes = [ReadOnlyOrManager]
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["name", "rate_bps", "created_at"]
    ordering = ["name"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return TaxRate.objects.none()
        return TaxRate.objects.filter(tenant=self.request.user.tenant).annotate(
            item_count=Count("items", distinct=True)
        )

    @transaction.atomic
    def perform_create(self, serializer):
        # Stand the current default down *before* saving, not after. The unique
        # constraint is checked on insert, so doing it afterwards means the
        # insert fails against the default that is still in place and the caller
        # gets a conflict for something they are entitled to do.
        if serializer.validated_data.get("is_default"):
            self._stand_down_other_defaults(self.request.user.tenant_id)

        rate = serializer.save(tenant=self.request.user.tenant)
        record_audit(
            action=AuditAction.CREATE,
            entity=rate,
            actor=self.request.user,
            request=self.request,
            after=diff_fields(rate, ["name", "rate_bps", "is_inclusive", "is_default"]),
        )

    @transaction.atomic
    def perform_update(self, serializer):
        before = diff_fields(
            serializer.instance, ["name", "rate_bps", "is_inclusive", "is_default", "is_active"]
        )
        if serializer.validated_data.get("is_default"):
            self._stand_down_other_defaults(
                self.request.user.tenant_id, exclude_pk=serializer.instance.pk
            )

        rate = serializer.save()
        record_audit(
            action=AuditAction.UPDATE,
            entity=rate,
            actor=self.request.user,
            request=self.request,
            before=before,
            after=diff_fields(
                rate, ["name", "rate_bps", "is_inclusive", "is_default", "is_active"]
            ),
        )

    @staticmethod
    def _stand_down_other_defaults(tenant_id, exclude_pk=None) -> None:
        """Clear the existing default so a new one can take its place.

        A partial unique index already forbids two defaults per business, and it
        is checked on write - so this has to run *before* the save it is making
        room for, not after. Both callers are inside a transaction, so a failure
        between the two steps leaves no business without a default.
        """
        queryset = TaxRate.objects.filter(tenant_id=tenant_id, is_default=True)
        if exclude_pk is not None:
            queryset = queryset.exclude(pk=exclude_pk)
        queryset.update(is_default=False)

    def perform_destroy(self, instance):
        if instance.items.exists():
            raise ValidationError(
                {
                    "detail": (
                        "This tax rate is used by items in your catalogue. "
                        "Deactivate it instead, so past sales keep their figures."
                    )
                }
            )
        record_audit(
            action=AuditAction.DELETE,
            entity=instance,
            actor=self.request.user,
            request=self.request,
            before=diff_fields(instance, ["name", "rate_bps"]),
        )
        instance.delete()


@extend_schema(tags=["catalog"])
class ItemViewSet(viewsets.ModelViewSet):
    """Everything the business sells, products and services alike."""

    permission_classes = [ReadOnlyOrManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "short_name", "sku", "barcodes__code"]
    ordering_fields = ["name", "sort_order", "price_cents", "created_at"]
    ordering = ["sort_order", "name"]
    filterset_fields = ["category", "item_type", "is_active", "is_available", "track_stock"]

    def get_queryset(self):
        """Items of the caller's business, with barcodes and stock prefetched.

        The prefetches matter: the till lists a whole catalogue at once, and
        without them each row would fetch its own barcodes and stock levels -
        hundreds of queries over a connection that is often slow.
        """
        if getattr(self, "swagger_fake_view", False):
            return Item.objects.none()

        from apps.inventory.models import StockItem

        return (
            Item.objects.filter(tenant=self.request.user.tenant)
            .select_related("category", "tax_rate")
            .prefetch_related(
                "barcodes",
                Prefetch(
                    "stock_levels",
                    queryset=StockItem.objects.select_related("store"),
                    to_attr="prefetched_stock",
                ),
            )
        )

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ItemWriteSerializer
        return ItemSerializer

    @extend_schema(
        summary="List items",
        description=(
            "The catalogue, filtered and searchable. `search` matches name, "
            "short name, SKU and barcode, so one box on the till serves every "
            "way a cashier might look something up."
        ),
        responses={200: ItemSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Add an item",
        request=ItemWriteSerializer,
        responses={201: ItemSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = serializer.save()

        record_audit(
            action=AuditAction.CREATE,
            entity=item,
            actor=request.user,
            request=request,
            after=diff_fields(item, ["sku", "name", "price_cents", "item_type"]),
        )
        return Response(
            ItemSerializer(item, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Update an item",
        request=ItemWriteSerializer,
        responses={200: ItemSerializer},
    )
    def update(self, request, *args, **kwargs):
        return self._update(request, partial=kwargs.pop("partial", False))

    def partial_update(self, request, *args, **kwargs):
        return self._update(request, partial=True)

    def _update(self, request, *, partial: bool):
        """Shared update path, so a price change is audited either way.

        A price change is the single most sensitive edit in the catalogue -
        it is how a dishonest manager would quietly move margin - so the before
        and after are always recorded.
        """
        instance = self.get_object()
        before = diff_fields(
            instance, ["sku", "name", "price_cents", "cost_cents", "is_active", "is_available"]
        )

        serializer = ItemWriteSerializer(
            instance, data=request.data, partial=partial, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        item = serializer.save()

        record_audit(
            action=AuditAction.UPDATE,
            entity=item,
            actor=request.user,
            request=request,
            before=before,
            after=diff_fields(
                item, ["sku", "name", "price_cents", "cost_cents", "is_active", "is_available"]
            ),
        )
        return Response(ItemSerializer(item, context=self.get_serializer_context()).data)

    def perform_destroy(self, instance):
        """Deactivate rather than delete.

        An item with sales behind it cannot be removed without orphaning them,
        and the till only ever needs it gone from view. Returning the
        deactivated item rather than an empty 204 lets the client update in
        place.
        """
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        record_audit(
            action=AuditAction.DEACTIVATE,
            entity=instance,
            actor=self.request.user,
            request=self.request,
            before={"is_active": True},
            after={"is_active": False},
        )

    @extend_schema(
        summary="Look an item up by barcode",
        description=(
            "Resolves any barcode on an item, not just its primary one. The "
            "same product genuinely arrives with different codes - a supplier "
            "changes packaging, a multipack carries its own - and all of them "
            "must scan.\n\n"
            "Returns 404 when nothing matches, which the till shows as "
            "'unknown barcode, add this item?'."
        ),
        parameters=[
            OpenApiParameter(
                "barcode",
                str,
                required=True,
                description="The scanned code, exactly as read.",
            )
        ],
        responses={200: ItemSerializer, 404: OpenApiResponse(description="No such barcode")},
    )
    @action(detail=False, methods=["get"])
    def lookup(self, request):
        code = (request.query_params.get("barcode") or "").strip()
        if not code:
            return Response(
                {"detail": "A barcode is required.", "code": "bad_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        item = self.get_queryset().filter(barcodes__code=code).first()
        if item is None:
            return Response(
                {
                    "detail": "No item in this business has that barcode.",
                    "code": "barcode_not_found",
                    "barcode": code,
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(ItemSerializer(item, context=self.get_serializer_context()).data)

    @extend_schema(
        summary="Search items for the till",
        description=(
            "A trimmed payload for type-ahead at the counter: enough to show a "
            "row and add it to a sale, without the stock and barcode detail the "
            "full listing carries."
        ),
        parameters=[OpenApiParameter("q", str, required=True)],
        responses={200: OpenApiResponse(description="Matching items, trimmed")},
    )
    @action(detail=False, methods=["get"])
    def search(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < 2:
            return Response(
                {"detail": "Type at least two characters.", "code": "query_too_short"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        matches = (
            self.get_queryset()
            .filter(
                Q(name__icontains=query)
                | Q(short_name__icontains=query)
                | Q(sku__icontains=query)
                | Q(barcodes__code__startswith=query)
            )
            .filter(is_active=True)
            .distinct()[:25]
        )

        return Response(
            [
                {
                    "id": str(item.id),
                    "sku": item.sku,
                    "name": item.name,
                    "till_label": item.till_label,
                    "price_cents": item.price_cents,
                    "is_price_variable": item.is_price_variable,
                    "is_available": item.is_available,
                    "unit": item.unit,
                }
                for item in matches
            ]
        )

    @extend_schema(
        summary="Download the import template",
        description=(
            "A CSV with every column and two worked examples - one product and "
            "one service. The service row is the one people get stuck on: it "
            "must not track stock, and may carry a duration and a variable price."
        ),
        responses={200: OpenApiResponse(description="text/csv")},
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="import/template",
        permission_classes=[IsManagerOrAbove],
    )
    def import_template(self, request):
        response = HttpResponse(build_template_csv(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="item-import-template.csv"'
        return response

    @extend_schema(
        summary="Check an import file without writing anything",
        description=(
            "Reads the file and reports what is wrong with it, row by row. "
            "Nothing is written.\n\n"
            "Returns a token that commit must present. The token is tied to a "
            "hash of this exact file and expires in an hour, so the rows that "
            "get imported are the rows whose report you read."
        ),
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
            }
        },
        responses={200: OpenApiResponse(description="Per-row report and a token")},
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="import/validate",
        parser_classes=[MultiPartParser],
        permission_classes=[IsManagerOrAbove],
    )
    def import_validate(self, request):
        raw, error = self._read_upload(request)
        if error is not None:
            return error

        try:
            rows, _headers = read_csv(raw)
        except ImportError_ as exc:
            return Response(
                {"detail": str(exc), "code": "unreadable_file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parsed = parse_rows(rows)
        check_against_catalogue(request.user.tenant, parsed)
        report = build_report(parsed)
        report.token = issue_token(request.user.tenant_id, request.user.id, raw)

        return Response(report.as_dict())

    @extend_schema(
        summary="Import the rows that passed",
        description=(
            "Imports every valid row and reports the rest. Valid rows go in "
            "even when others fail - refusing four hundred good rows over three "
            "bad ones means an afternoon of spreadsheet editing.\n\n"
            "Items are matched by SKU, so re-uploading a corrected file fixes "
            "rows rather than duplicating them.\n\n"
            "Every category and tax rate a row names is resolved again here, "
            "not carried over from the check. If one was renamed or deleted in "
            "between, that row fails like any other bad reference while the "
            "rest still import."
        ),
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "token": {"type": "string"},
                    "store": {"type": "string", "description": "Where opening stock lands."},
                },
            }
        },
        responses={
            200: OpenApiResponse(description="What was imported, and what was not"),
            400: OpenApiResponse(description="Token expired, or file does not match"),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="import/commit",
        parser_classes=[MultiPartParser],
        permission_classes=[IsManagerOrAbove],
    )
    def import_commit(self, request):
        raw, error = self._read_upload(request)
        if error is not None:
            return error

        token = (request.data.get("token") or "").strip()
        if not token:
            return Response(
                {
                    "detail": "Check the file first; commit needs the token that returns.",
                    "code": "token_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        token_error = consume_token(token, request.user.tenant_id, raw)
        if token_error is not None:
            return Response(
                {"detail": token_error, "code": "token_invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            rows, _headers = read_csv(raw)
        except ImportError_ as exc:
            return Response(
                {"detail": str(exc), "code": "unreadable_file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        store, store_error = self._resolve_import_store(request)
        if store_error is not None:
            return store_error

        parsed = parse_rows(rows)
        # Re-checked against the catalogue as it is *now*, not as it was when
        # the report was produced. A tax rate renamed in between must fail its
        # row rather than import against the wrong one.
        check_against_catalogue(request.user.tenant, parsed)

        report = commit_rows(
            tenant=request.user.tenant, user=request.user, parsed=parsed, store=store
        )

        record_audit(
            action=AuditAction.CREATE,
            entity_type="catalog.Item",
            entity_id="bulk-import",
            actor=request.user,
            request=request,
            reason="Catalogue import",
            after={
                "created": report.created,
                "updated": report.updated,
                "rejected": report.invalid,
                "categories_created": report.categories_created,
            },
        )
        return Response(report.as_dict())

    @staticmethod
    def _read_upload(request):
        """Pull the uploaded file out of the request, or explain what is missing."""
        upload = request.FILES.get("file")
        if upload is None:
            return None, Response(
                {"detail": "Attach a CSV file as 'file'.", "code": "file_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size > 10 * 1024 * 1024:
            return None, Response(
                {"detail": "That file is over 10MB.", "code": "file_too_large"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return upload.read(), None

    def _resolve_import_store(self, request):
        """Decide which branch opening quantities belong to.

        Defaults to the business's default branch. With several branches and no
        choice made, this refuses rather than guessing - putting a delivery on
        the wrong branch's shelf is worse than asking.
        """
        from apps.stores.models import Store

        requested = request.data.get("store")
        stores = Store.objects.filter(tenant=request.user.tenant, is_active=True)

        if requested:
            store = stores.filter(pk=requested).first()
            if store is None:
                return None, Response(
                    {"detail": "No such branch.", "code": "not_found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return store, None

        default = stores.filter(is_default=True).first()
        if default is not None:
            return default, None

        if stores.count() > 1:
            return None, Response(
                {
                    "detail": (
                        "This business has several branches and none is marked "
                        "default. Say which one the opening stock belongs to."
                    ),
                    "code": "store_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return stores.first(), None

    @extend_schema(
        summary="Add a barcode to an item",
        request=BarcodeSerializer,
        responses={201: BarcodeSerializer},
    )
    @action(detail=True, methods=["post"], url_path="barcodes")
    def add_barcode(self, request, pk=None):
        item = self.get_object()
        serializer = BarcodeSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)

        barcode = serializer.save(tenant=request.user.tenant, item=item)
        record_audit(
            action=AuditAction.CREATE,
            entity=barcode,
            actor=request.user,
            request=request,
            after={"code": barcode.code, "item": str(item.pk)},
        )
        return Response(BarcodeSerializer(barcode).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Remove a barcode from an item",
        responses={204: OpenApiResponse(description="Removed")},
    )
    @action(detail=True, methods=["delete"], url_path=r"barcodes/(?P<barcode_id>[^/.]+)")
    def remove_barcode(self, request, pk=None, barcode_id=None):
        item = self.get_object()
        barcode = Barcode.objects.filter(item=item, pk=barcode_id).first()
        if barcode is None:
            return Response(
                {"detail": "No such barcode on this item.", "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        record_audit(
            action=AuditAction.DELETE,
            entity=barcode,
            actor=request.user,
            request=request,
            before={"code": barcode.code, "item": str(item.pk)},
        )
        barcode.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
