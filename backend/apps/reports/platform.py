"""
What each business owes for, across every business.

The one place in this system where a reporting bug becomes a **billing** bug, so
it gets the treatment money gets: integer cents, its own tests, and a bypass
window kept to the narrowest statement that can do the work.

Reads across tenants, which nothing else in reporting does. That means
``bypass_rls()``, and the discipline that goes with it: the bypass wraps the
queries and nothing else, so no view logic, no serialization and no third-party
call ever runs with isolation off.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounts.models import Device, User
from apps.core.tenancy import bypass_rls
from apps.reports.periods import Period
from apps.reports.queries import REVENUE_STATES
from apps.sales.models import Payment, PaymentMethod, Sale
from apps.tenants.models import Tenant

_ZERO = Value(0)


@dataclass(frozen=True)
class TenantUsage:
    """One business's period, in the terms an invoice is written from."""

    tenant_id: str
    name: str
    slug: str
    status: str

    sale_count: int = 0
    gross_cents: int = 0
    cash_cents: int = 0
    mpesa_cents: int = 0

    active_device_count: int = 0
    active_user_count: int = 0

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "slug": self.slug,
            "status": self.status,
            "sale_count": self.sale_count,
            "gross_cents": self.gross_cents,
            "cash_cents": self.cash_cents,
            "mpesa_cents": self.mpesa_cents,
            "active_device_count": self.active_device_count,
            "active_user_count": self.active_user_count,
        }


def usage_summary(period: Period) -> list[TenantUsage]:
    """Every business's usage for a period, ordered by what they took.

    **Suspended businesses are included.** A business suspended halfway through
    a month still traded for the part before, and still owes for it. Dropping
    them would quietly forgive an invoice, and the operator would have no way of
    noticing which one.

    Businesses that took nothing are included too, at zero. An absence and a
    zero are different facts, and an invoicing run needs to see the difference
    between "did not trade" and "is not in the list".
    """
    # The bypass covers the reads and nothing else. Everything after it -
    # assembling the dataclasses, ordering, whatever a caller does next - runs
    # with isolation back on.
    with bypass_rls():
        tenants = list(Tenant.objects.all().order_by("name"))

        sales = {
            row["tenant_id"]: row
            for row in Sale.all_objects.filter(
                state__in=REVENUE_STATES,
                server_received_at__gte=period.start,
                server_received_at__lt=period.end,
            )
            .values("tenant_id")
            .annotate(count=Count("id"), gross=Coalesce(Sum("total_cents"), _ZERO))
        }

        payments = {
            row["tenant_id"]: row
            for row in Payment.all_objects.filter(
                created_at__gte=period.start, created_at__lt=period.end
            )
            .values("tenant_id")
            .annotate(
                cash=Coalesce(
                    Sum("amount_cents", filter=Q(method=PaymentMethod.CASH)), _ZERO
                ),
                mpesa=Coalesce(
                    Sum("amount_cents", filter=Q(method=PaymentMethod.MPESA)), _ZERO
                ),
            )
        }

        devices = dict(
            Device.all_objects.filter(is_active=True)
            .values_list("tenant_id")
            .annotate(count=Count("id"))
        )
        users = dict(
            User.all_objects.filter(is_active=True, tenant__isnull=False)
            .values_list("tenant_id")
            .annotate(count=Count("id"))
        )

    rows = []
    for tenant in tenants:
        sale = sales.get(tenant.id, {})
        payment = payments.get(tenant.id, {})
        rows.append(
            TenantUsage(
                tenant_id=str(tenant.id),
                name=tenant.name,
                slug=tenant.slug,
                status=tenant.status,
                sale_count=sale.get("count", 0),
                gross_cents=sale.get("gross", 0),
                cash_cents=payment.get("cash", 0),
                mpesa_cents=payment.get("mpesa", 0),
                active_device_count=devices.get(tenant.id, 0),
                active_user_count=users.get(tenant.id, 0),
            )
        )

    return sorted(rows, key=lambda row: (-row.gross_cents, row.name))


@dataclass(frozen=True)
class PlatformTotals:
    tenant_count: int = 0
    trading_tenant_count: int = 0
    sale_count: int = 0
    gross_cents: int = 0

    def as_dict(self) -> dict:
        return {
            "tenant_count": self.tenant_count,
            "trading_tenant_count": self.trading_tenant_count,
            "sale_count": self.sale_count,
            "gross_cents": self.gross_cents,
        }


def platform_totals(rows: list[TenantUsage]) -> PlatformTotals:
    """The whole platform, added up.

    ``trading_tenant_count`` counts businesses that actually sold something,
    which is not the same as the number provisioned - and it is the figure the
    operator cares about when deciding whether a month went well.
    """
    return PlatformTotals(
        tenant_count=len(rows),
        trading_tenant_count=sum(1 for row in rows if row.sale_count),
        sale_count=sum(row.sale_count for row in rows),
        gross_cents=sum(row.gross_cents for row in rows),
    )
