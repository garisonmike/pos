"""
Bulk item import from CSV.

A client's product list is reliably the slowest part of onboarding - not the
software. So this is built for the shape that work actually takes: someone
exports a spreadsheet from wherever their prices currently live, it is
inconsistent in a dozen small ways, and they need to be told exactly which rows
are wrong rather than that "the import failed".

Hence two phases. **Validate** reads the file, checks every row, and writes
nothing. **Commit** imports the rows that passed and reports the rest. Valid
rows go in even when others fail, because refusing four hundred good rows over
three bad ones means a shop owner editing a spreadsheet all afternoon.

Two details are there to stop the phases disagreeing:

* The token returned by validate is tied to a hash of the file, so commit cannot
  be pointed at a *different* file than the one whose report was reviewed.
* Commit re-resolves every category and tax rate a row names, rather than
  trusting what validate found. Between the two calls someone may have renamed
  or deleted a tax rate, and a row referencing it must fail like any other bad
  reference - not import silently against the wrong rate, and not abort the
  whole file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.utils.text import slugify

from apps.catalog.models import Barcode, Category, Item, ItemType, TaxRate, UnitOfMeasure
from apps.core.money import to_cents

#: How long a reviewed report stays good for. Long enough to read it and think,
#: short enough that a token found later is worthless.
TOKEN_TTL_SECONDS = 3600

#: Refuse anything larger rather than time out halfway through. Past this size
#: the honest answer is a background job, and carrying a worker before any real
#: catalogue needs one would be cost without benefit.
MAX_ROWS = 5000

#: Several barcodes in one cell, because a case pack and a single unit are the
#: same product with two codes.
BARCODE_SEPARATOR = ";"

REQUIRED_COLUMNS = ("sku", "name", "price")

OPTIONAL_COLUMNS = (
    "short_name",
    "description",
    "category",
    "tax_rate",
    "cost",
    "unit",
    "item_type",
    "track_stock",
    "is_price_variable",
    "duration_minutes",
    "barcodes",
    "opening_quantity",
    "reorder_level",
    "sort_order",
)

TRUE_VALUES = {"1", "true", "yes", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "n", "f", ""}


@dataclass
class RowError:
    """One thing wrong with one row, phrased so it can be acted on."""

    row: int
    field: str
    message: str
    sku: str = ""

    def as_dict(self) -> dict:
        return {"row": self.row, "sku": self.sku, "field": self.field, "message": self.message}


@dataclass
class ParsedRow:
    """One CSV line, cleaned up, with whatever was wrong with it."""

    row_number: int
    sku: str
    values: dict
    errors: list[RowError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass
class ImportReport:
    """What happened, or what would happen."""

    total: int = 0
    valid: int = 0
    invalid: int = 0
    created: int = 0
    updated: int = 0
    errors: list[RowError] = field(default_factory=list)
    categories_created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    token: str = ""

    def as_dict(self) -> dict:
        payload = {
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "errors": [error.as_dict() for error in self.errors],
            "categories_created": self.categories_created,
            "warnings": self.warnings,
        }
        if self.token:
            payload["token"] = self.token
            payload["token_expires_in_seconds"] = TOKEN_TTL_SECONDS
        if self.created or self.updated:
            payload["created"] = self.created
            payload["updated"] = self.updated
        return payload


class ImportError_(Exception):
    """The file as a whole cannot be processed, as opposed to a bad row."""


def read_csv(raw: bytes) -> tuple[list[dict], list[str]]:
    """Decode and parse the upload, tolerating what spreadsheets actually emit.

    Excel on a Windows machine writes UTF-8 with a byte order mark and CRLF line
    endings; ``utf-8-sig`` handles the first and the csv module the second.
    Latin-1 is the fallback because it never raises, which turns an unreadable
    file into a few odd characters in one name rather than a failed import.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_("That file appears to be empty.")

    headers = [(name or "").strip().lower().replace(" ", "_") for name in reader.fieldnames]
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise ImportError_(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Required: {', '.join(REQUIRED_COLUMNS)}."
        )

    rows = []
    for raw_row in reader:
        rows.append(
            {
                (key or "").strip().lower().replace(" ", "_"): (value or "").strip()
                for key, value in raw_row.items()
            }
        )
        if len(rows) > MAX_ROWS:
            raise ImportError_(
                f"That file has more than {MAX_ROWS} rows. Split it, or ask for "
                "a background import."
            )
    return rows, headers


def _parse_money(value: str, row: int, sku: str, column: str) -> tuple[int | None, RowError | None]:
    """Turn a price cell into integer cents, or explain why it will not.

    The letter O in place of a zero is called out by name because it is the most
    common way a hand-edited price list goes wrong, and "invalid decimal" would
    send someone hunting for it.
    """
    if not value:
        return None, RowError(row, column, "Required.", sku)

    cleaned = value.replace(",", "").replace("KES", "").replace("Ksh", "").strip()
    if "O" in cleaned or "o" in cleaned:
        return None, RowError(
            row, column, f"Contains the letter O rather than a zero: {value!r}.", sku
        )
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None, RowError(row, column, f"Not a valid amount: {value!r}.", sku)

    if amount < 0:
        return None, RowError(row, column, "Cannot be negative.", sku)
    return to_cents(amount), None


def _parse_decimal(
    value: str, row: int, sku: str, column: str, default: Decimal = Decimal("0")
) -> tuple[Decimal | None, RowError | None]:
    if not value:
        return default, None
    try:
        return Decimal(value.replace(",", "")), None
    except (InvalidOperation, ValueError):
        return None, RowError(row, column, f"Not a valid number: {value!r}.", sku)


def _parse_bool(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in TRUE_VALUES


def parse_rows(rows: list[dict]) -> list[ParsedRow]:
    """Clean and check every row on its own terms, before touching the database.

    Only field-level checks happen here. Anything needing a lookup - does this
    category exist, is this barcode taken - is done later, so that this stays
    cheap and so the database work happens once per phase rather than per row.
    """
    parsed = []

    for index, raw in enumerate(rows, start=2):  # row 1 is the header
        sku = raw.get("sku", "").strip()
        errors: list[RowError] = []
        values: dict = {}

        if not sku:
            errors.append(RowError(index, "sku", "Required.", ""))
        values["sku"] = sku

        name = raw.get("name", "").strip()
        if not name:
            errors.append(RowError(index, "name", "Required.", sku))
        values["name"] = name
        values["short_name"] = raw.get("short_name", "")[:24]
        values["description"] = raw.get("description", "")

        price_cents, error = _parse_money(raw.get("price", ""), index, sku, "price")
        if error:
            errors.append(error)
        values["price_cents"] = price_cents

        if raw.get("cost"):
            cost_cents, error = _parse_money(raw["cost"], index, sku, "cost")
            if error:
                errors.append(error)
            values["cost_cents"] = cost_cents
        else:
            values["cost_cents"] = 0

        item_type = (raw.get("item_type") or ItemType.PRODUCT).strip().upper()
        if item_type not in ItemType.values:
            errors.append(
                RowError(
                    index,
                    "item_type",
                    f"Must be one of: {', '.join(ItemType.values)}.",
                    sku,
                )
            )
            item_type = ItemType.PRODUCT
        values["item_type"] = item_type

        unit = (raw.get("unit") or UnitOfMeasure.EACH).strip().upper()
        if unit not in UnitOfMeasure.values:
            errors.append(
                RowError(
                    index, "unit", f"Must be one of: {', '.join(UnitOfMeasure.values)}.", sku
                )
            )
            unit = UnitOfMeasure.EACH
        values["unit"] = unit

        # A service has no shelf, so tracking stock on one is always a mistake
        # rather than a preference. Defaulted rather than rejected, since the
        # column is usually absent for service rows entirely.
        is_service = item_type == ItemType.SERVICE
        values["track_stock"] = False if is_service else _parse_bool(
            raw.get("track_stock", ""), True
        )
        if is_service and _parse_bool(raw.get("track_stock", ""), False):
            errors.append(
                RowError(
                    index,
                    "track_stock",
                    "A service cannot track stock. Leave this blank or set it to no.",
                    sku,
                )
            )

        values["is_price_variable"] = _parse_bool(raw.get("is_price_variable", ""), False)

        duration = raw.get("duration_minutes", "")
        if duration:
            try:
                values["duration_minutes"] = int(duration)
            except ValueError:
                errors.append(
                    RowError(index, "duration_minutes", f"Not a whole number: {duration!r}.", sku)
                )
                values["duration_minutes"] = None
        else:
            values["duration_minutes"] = None

        sort_order = raw.get("sort_order", "")
        try:
            values["sort_order"] = int(sort_order) if sort_order else 0
        except ValueError:
            errors.append(
                RowError(index, "sort_order", f"Not a whole number: {sort_order!r}.", sku)
            )
            values["sort_order"] = 0

        codes = [
            code.strip()
            for code in raw.get("barcodes", "").split(BARCODE_SEPARATOR)
            if code.strip()
        ]
        values["barcodes"] = codes

        quantity, error = _parse_decimal(
            raw.get("opening_quantity", ""), index, sku, "opening_quantity"
        )
        if error:
            errors.append(error)
        values["opening_quantity"] = quantity

        reorder, error = _parse_decimal(
            raw.get("reorder_level", ""), index, sku, "reorder_level"
        )
        if error:
            errors.append(error)
        values["reorder_level"] = reorder

        values["category_name"] = raw.get("category", "").strip()
        values["tax_rate_name"] = raw.get("tax_rate", "").strip()

        parsed.append(ParsedRow(row_number=index, sku=sku, values=values, errors=errors))

    return parsed


def check_against_catalogue(tenant, parsed: list[ParsedRow]) -> None:
    """Add the errors that need the existing catalogue to detect.

    Run in both phases. At validate it produces the report; at commit it runs
    again against the catalogue as it is *then*, which is what catches a tax
    rate renamed or deleted since the report was read.
    """
    seen_skus: dict[str, int] = {}
    for row in parsed:
        if not row.sku:
            continue
        if row.sku in seen_skus:
            row.errors.append(
                RowError(
                    row.row_number,
                    "sku",
                    f"Repeated in this file; first seen on row {seen_skus[row.sku]}.",
                    row.sku,
                )
            )
        else:
            seen_skus[row.sku] = row.row_number

    # Tax rates must already exist. A typo silently creating "VAT 16 %" at a
    # wrong value would mis-tax every sale filed against it ever after, which is
    # not a mistake worth trading for a faster first import.
    wanted_rates = {row.values["tax_rate_name"] for row in parsed if row.values["tax_rate_name"]}
    known_rates = {
        rate.name.lower(): rate
        for rate in TaxRate.objects.filter(tenant=tenant, name__in=wanted_rates)
    }
    known_rates.update(
        {
            rate.name.lower(): rate
            for rate in TaxRate.objects.filter(tenant=tenant, is_active=True)
        }
    )

    for row in parsed:
        wanted = row.values["tax_rate_name"]
        if wanted and wanted.lower() not in known_rates:
            row.errors.append(
                RowError(
                    row.row_number,
                    "tax_rate",
                    f"Unknown tax rate {wanted!r}. Create it under tax settings "
                    "first, so the rate and its inclusive/exclusive setting are "
                    "deliberate.",
                    row.sku,
                )
            )
        row.values["_tax_rate"] = known_rates.get(wanted.lower()) if wanted else None

    # Barcodes already used by a different item.
    all_codes = [code for row in parsed for code in row.values["barcodes"]]
    taken = {
        barcode.code: barcode
        for barcode in Barcode.objects.filter(tenant=tenant, code__in=all_codes).select_related(
            "item"
        )
    }

    seen_codes: dict[str, int] = {}
    for row in parsed:
        for code in row.values["barcodes"]:
            if code in seen_codes and seen_codes[code] != row.row_number:
                row.errors.append(
                    RowError(
                        row.row_number,
                        "barcodes",
                        f"{code} also appears on row {seen_codes[code]}.",
                        row.sku,
                    )
                )
            seen_codes.setdefault(code, row.row_number)

            existing = taken.get(code)
            if existing is not None and existing.item.sku != row.sku:
                row.errors.append(
                    RowError(
                        row.row_number,
                        "barcodes",
                        f"{code} already belongs to {existing.item.name}.",
                        row.sku,
                    )
                )


def build_report(parsed: list[ParsedRow]) -> ImportReport:
    """Collect per-row errors into the summary the client renders."""
    report = ImportReport(total=len(parsed))
    for row in parsed:
        if row.is_valid:
            report.valid += 1
        else:
            report.invalid += 1
            report.errors.extend(row.errors)
    return report


def issue_token(tenant_id, user_id, raw: bytes) -> str:
    """Mint a token tying a reviewed report to the exact file it described.

    The file's hash is part of what is stored, so committing a *different* file
    with a valid token is refused. Without that, the report someone approved and
    the rows that actually get imported could be two different things.
    """
    token = secrets.token_urlsafe(24)
    cache.set(
        f"item-import:{token}",
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "file_hash": hashlib.sha256(raw).hexdigest(),
        },
        TOKEN_TTL_SECONDS,
    )
    return token


def consume_token(token: str, tenant_id, raw: bytes) -> str | None:
    """Check a token against the file being committed. Returns an error, or None.

    Deliberately not deleted on success: a commit that fails partway is worth
    retrying, and forcing a re-upload and a re-read of the report would be
    punishing an error the person did not make. It expires on its own.
    """
    stored = cache.get(f"item-import:{token}")
    if stored is None:
        return (
            "That import has expired or was already finished. Upload the file "
            "again to get a fresh report."
        )
    if stored["tenant_id"] != str(tenant_id):
        return "That import token does not belong to this business."
    if stored["file_hash"] != hashlib.sha256(raw).hexdigest():
        return (
            "This file is not the one that was checked. Upload it again so the "
            "report matches what will be imported."
        )
    return None


@transaction.atomic
def commit_rows(*, tenant, user, parsed: list[ParsedRow], store=None) -> ImportReport:
    """Write the valid rows. Invalid ones are reported and skipped.

    Categories named by a row are created if missing, because a category is a
    free-form label and making someone pre-create thirty of them before their
    first import is friction with nothing to show for it. Tax rates are not,
    for the reason given above.
    """
    report = build_report(parsed)

    for row in parsed:
        if not row.is_valid:
            continue

        values = row.values
        category = None
        if values["category_name"]:
            category, created = _get_or_create_category(tenant, values["category_name"])
            if created:
                report.categories_created.append(category.name)

        item, was_created = Item.objects.update_or_create(
            tenant=tenant,
            sku=values["sku"],
            defaults={
                "name": values["name"],
                "short_name": values["short_name"],
                "description": values["description"],
                "category": category,
                "item_type": values["item_type"],
                "unit": values["unit"],
                "price_cents": values["price_cents"],
                "cost_cents": values["cost_cents"],
                "tax_rate": values.get("_tax_rate"),
                "track_stock": values["track_stock"],
                "is_price_variable": values["is_price_variable"],
                "duration_minutes": values["duration_minutes"],
                "sort_order": values["sort_order"],
                "created_by": user,
            },
        )
        report.created += int(was_created)
        report.updated += int(not was_created)

        _sync_barcodes(tenant, item, values["barcodes"])

        if item.track_stock and store is not None:
            _apply_opening_stock(tenant, item, store, values, user)

    if report.categories_created:
        report.categories_created = sorted(set(report.categories_created))
    return report


def _get_or_create_category(tenant, name: str) -> tuple[Category, bool]:
    """Find a category by name within the business, or create it.

    Matched case-insensitively so "Dry Goods" and "dry goods" in the same file
    do not become two categories.
    """
    existing = Category.objects.filter(tenant=tenant, name__iexact=name).first()
    if existing is not None:
        return existing, False

    slug = slugify(name)[:110] or "category"
    suffix = 2
    while Category.objects.filter(tenant=tenant, slug=slug).exists():
        slug = f"{slugify(name)[:100]}-{suffix}"
        suffix += 1

    return Category.objects.create(tenant=tenant, name=name, slug=slug), True


def _sync_barcodes(tenant, item: Item, codes: list[str]) -> None:
    """Attach any codes not already on this item, first one primary.

    Additive rather than replacing, because a file listing one code for an item
    that already has three usually means "here is the one I know", not "delete
    the others".
    """
    existing = set(item.barcodes.values_list("code", flat=True))
    for index, code in enumerate(codes):
        if code in existing:
            continue
        Barcode.objects.create(
            tenant=tenant, item=item, code=code, is_primary=index == 0 and not existing
        )


def _apply_opening_stock(tenant, item: Item, store, values: dict, user) -> None:
    """Set an opening quantity and reorder level, through the ledger.

    Written as a movement rather than straight into the total so that even an
    imported opening figure can be explained afterwards. Re-importing a file
    sets the count to the figure in it rather than adding to what is there,
    which is what someone correcting a spreadsheet expects.
    """
    from apps.inventory.models import MovementReason, StockItem, apply_movement

    stock_item, _created = StockItem.objects.get_or_create(
        tenant=tenant,
        item=item,
        store=store,
        defaults={"reorder_level": values["reorder_level"] or Decimal("0")},
    )

    if values["reorder_level"] is not None:
        stock_item.reorder_level = values["reorder_level"]
        stock_item.save(update_fields=["reorder_level", "updated_at"])

    opening = values["opening_quantity"]
    if opening is None:
        return

    delta = opening - stock_item.quantity
    if delta == 0:
        return

    apply_movement(
        stock_item=stock_item,
        delta=delta,
        reason=MovementReason.COUNT,
        user=user,
        note="Set by catalogue import.",
        ref_type="catalog.import",
    )


CSV_TEMPLATE_HEADERS = list(REQUIRED_COLUMNS) + list(OPTIONAL_COLUMNS)

CSV_TEMPLATE_EXAMPLE_ROWS = [
    {
        "sku": "SUGAR-1KG",
        "name": "Sugar 1kg",
        "price": "180.00",
        "short_name": "Sugar 1kg",
        "category": "Dry Goods",
        "tax_rate": "VAT 16%",
        "cost": "150.00",
        "unit": "EACH",
        "item_type": "PRODUCT",
        "track_stock": "yes",
        "barcodes": "6161100234567;6161100234574",
        "opening_quantity": "40",
        "reorder_level": "10",
    },
    {
        "sku": "SVC-BRAID",
        "name": "Braiding",
        "price": "500.00",
        "category": "Services",
        "item_type": "SERVICE",
        "track_stock": "no",
        "is_price_variable": "yes",
        "duration_minutes": "120",
    },
]


def build_template_csv() -> str:
    """A starter file with every column and two worked examples.

    One product and one service, because the service row is where people get
    stuck: it is the one that must not track stock and may carry a duration and
    a variable price.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_TEMPLATE_HEADERS)
    writer.writeheader()
    for row in CSV_TEMPLATE_EXAMPLE_ROWS:
        writer.writerow({column: row.get(column, "") for column in CSV_TEMPLATE_HEADERS})
    return buffer.getvalue()
