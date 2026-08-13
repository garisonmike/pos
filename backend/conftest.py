"""
Shared test fixtures.

Two businesses are set up for most tests rather than one, because almost every
question worth asking about this system is "can A see B's data". A single
tenant would let an isolation bug pass unnoticed simply because there was
nothing to leak.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import transaction
from rest_framework.test import APIClient

from apps.accounts.constants import UserRole
from apps.accounts.models import Device, User
from apps.accounts.tokens import issue_tokens_for
from apps.catalog.models import Item, TaxRate
from apps.core.tenancy import bypass_rls, tenant_context
from apps.stores.models import Store
from apps.tenants.models import BusinessType, TenantStatus
from apps.tenants.services import provision_tenant


@pytest.fixture(autouse=True)
def clear_cache():
    """Start every test with an empty cache.

    The cache holds till lockout counters and per-business suspension status.
    Neither is rolled back with the database, so without this a test that
    exhausts a device's PIN attempts would leave the next one locked out, and
    the failure would appear in a test that never mentioned lockout.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def platform_admin(db) -> User:
    """The platform operator. Belongs to no business."""
    with transaction.atomic(), bypass_rls():
        return User.objects.create_superuser(
            username="platform-op",
            password="platform-pass-9271",
            full_name="Platform operator",
            email="ops@example.com",
        )


def _provision(name: str, slug: str, business_type: str = BusinessType.RETAIL):
    """Create a business with an owner, the way the platform console does."""
    with transaction.atomic(), bypass_rls():
        return provision_tenant(
            name=name,
            slug=slug,
            business_type=business_type,
            status=TenantStatus.ACTIVE,
            owner_username="owner",
            owner_full_name=f"{name} owner",
            owner_password="owner-pass-8812",
        )


@pytest.fixture
def tenant_a(db):
    """A retail business."""
    tenant, _owner = _provision("Mama Njeri Duka", "mama-njeri")
    return tenant


@pytest.fixture
def tenant_b(db):
    """A second, unrelated business. Exists to be leaked to, and never be."""
    tenant, _owner = _provision("Kwa Baba Hardware", "kwa-baba")
    return tenant


@pytest.fixture
def owner_a(tenant_a) -> User:
    with transaction.atomic(), tenant_context(tenant_a.id):
        return User.objects.get(tenant=tenant_a, username="owner")


@pytest.fixture
def owner_b(tenant_b) -> User:
    with transaction.atomic(), tenant_context(tenant_b.id):
        return User.objects.get(tenant=tenant_b, username="owner")


def _make_user(tenant, username: str, role: str, pin: str = "") -> User:
    with transaction.atomic(), tenant_context(tenant.id):
        user = User(
            tenant=tenant,
            username=username,
            full_name=f"{username.title()} Test",
            role=role,
        )
        user.set_password("staff-pass-4471")
        if pin:
            user.set_pin(pin)
        user.save()
        return user


@pytest.fixture
def manager_a(tenant_a) -> User:
    return _make_user(tenant_a, "mngr", UserRole.MANAGER)


@pytest.fixture
def cashier_a(tenant_a) -> User:
    return _make_user(tenant_a, "mary", UserRole.CASHIER, pin="1234")


@pytest.fixture
def cashier_b(tenant_b) -> User:
    return _make_user(tenant_b, "mary", UserRole.CASHIER, pin="4321")


@pytest.fixture
def store_a(tenant_a) -> Store:
    with transaction.atomic(), tenant_context(tenant_a.id):
        return Store.objects.create(
            tenant=tenant_a, name="Main", code="MAIN", is_default=True
        )


@pytest.fixture
def store_b(tenant_b) -> Store:
    with transaction.atomic(), tenant_context(tenant_b.id):
        return Store.objects.create(
            tenant=tenant_b, name="Main", code="MAIN", is_default=True
        )


@pytest.fixture
def tax_rate_a(tenant_a) -> TaxRate:
    with transaction.atomic(), tenant_context(tenant_a.id):
        return TaxRate.objects.create(
            tenant=tenant_a, name="VAT 16%", rate_bps=1600, is_inclusive=True, is_default=True
        )


@pytest.fixture
def item_a(tenant_a, tax_rate_a) -> Item:
    with transaction.atomic(), tenant_context(tenant_a.id):
        return Item.objects.create(
            tenant=tenant_a,
            sku="SUGAR-1KG",
            name="Sugar 1kg",
            price_cents=18000,
            cost_cents=15000,
            tax_rate=tax_rate_a,
        )


@pytest.fixture
def exclusive_rate_a(tenant_a) -> TaxRate:
    """A tax-exclusive rate alongside the inclusive one.

    Both exist in the same business on purpose: mixing the two is the case the
    per-rate ``is_inclusive`` flag exists for, and the one a per-business
    setting could not express.
    """
    with transaction.atomic(), tenant_context(tenant_a.id):
        return TaxRate.objects.create(
            tenant=tenant_a, name="VAT 16% trade", rate_bps=1600, is_inclusive=False
        )


@pytest.fixture
def category_a(tenant_a):
    from apps.catalog.models import Category

    with transaction.atomic(), tenant_context(tenant_a.id):
        return Category.objects.create(tenant=tenant_a, name="Dry Goods", slug="dry-goods")


@pytest.fixture
def service_a(tenant_a):
    """A service: no stock, has a duration, price quoted on the day."""
    with transaction.atomic(), tenant_context(tenant_a.id):
        return Item.objects.create(
            tenant=tenant_a,
            sku="SVC-BRAID",
            name="Braiding",
            price_cents=50000,
            item_type="SERVICE",
            track_stock=False,
            is_price_variable=True,
            duration_minutes=120,
        )


@pytest.fixture
def stock_a(tenant_a, item_a, store_a):
    """Forty bags of sugar at the main branch, with a reorder level of ten."""
    from apps.inventory.models import MovementReason, StockItem, apply_movement

    with transaction.atomic(), tenant_context(tenant_a.id):
        stock = StockItem.objects.create(
            tenant=tenant_a, item=item_a, store=store_a, reorder_level=Decimal("10")
        )
        apply_movement(
            stock_item=stock,
            delta=Decimal("40"),
            reason=MovementReason.PURCHASE,
            note="Opening delivery",
        )
        stock.refresh_from_db()
        return stock


@pytest.fixture
def item_b(tenant_b):
    """An item in the other business, to be leaked to and never be."""
    with transaction.atomic(), tenant_context(tenant_b.id):
        return Item.objects.create(
            tenant=tenant_b, sku="NAILS-2IN", name="Nails 2 inch", price_cents=25000
        )


@pytest.fixture
def stock_b(tenant_b, item_b, store_b):
    from apps.inventory.models import StockItem

    with transaction.atomic(), tenant_context(tenant_b.id):
        return StockItem.objects.create(
            tenant=tenant_b, item=item_b, store=store_b, reorder_level=Decimal("5")
        )


@pytest.fixture
def device_a(tenant_a):
    """A registered till, returned with its one-time plaintext token."""
    with transaction.atomic(), tenant_context(tenant_a.id):
        return Device.issue(tenant=tenant_a, name="Front counter")


def authenticated_client(user: User) -> APIClient:
    """An API client carrying a real access token for this user.

    Tokens are issued through the same code path the sign-in endpoints use, so
    the tenant claim the middleware depends on is present exactly as it would
    be in production.
    """
    client = APIClient()
    with transaction.atomic():
        context = (
            tenant_context(user.tenant_id) if user.tenant_id else bypass_rls()
        )
        with context:
            tokens = issue_tokens_for(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return client


@pytest.fixture
def client_owner_a(owner_a) -> APIClient:
    return authenticated_client(owner_a)


@pytest.fixture
def client_owner_b(owner_b) -> APIClient:
    return authenticated_client(owner_b)


@pytest.fixture
def client_manager_a(manager_a) -> APIClient:
    return authenticated_client(manager_a)


@pytest.fixture
def client_cashier_a(cashier_a) -> APIClient:
    return authenticated_client(cashier_a)


@pytest.fixture
def client_platform(platform_admin) -> APIClient:
    return authenticated_client(platform_admin)


@pytest.fixture
def anon_client() -> APIClient:
    return APIClient()
