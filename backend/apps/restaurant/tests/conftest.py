"""Fixtures for a restaurant that has the module switched on."""

from __future__ import annotations

import pytest

from apps.catalog.models import Item, UnitOfMeasure
from apps.core.tenancy import tenant_context
from apps.restaurant.models import Modifier, ModifierGroup, Table
from apps.tenants.models import ModuleKey, TenantModule


@pytest.fixture
def restaurant(tenant_a):
    """A business with the restaurant module enabled."""
    with tenant_context(tenant_a.id):
        module, _created = TenantModule.objects.get_or_create(
            tenant=tenant_a,
            module_key=ModuleKey.RESTAURANT,
            defaults={"is_enabled": True},
        )
        if not module.is_enabled:
            module.is_enabled = True
            module.save()
        return tenant_a


@pytest.fixture
def table_four(restaurant, store_a):
    with tenant_context(restaurant.id):
        return Table.objects.create(
            tenant=restaurant, store=store_a, name="Table 4", seats=4
        )


@pytest.fixture
def table_six(restaurant, store_a):
    with tenant_context(restaurant.id):
        return Table.objects.create(
            tenant=restaurant, store=store_a, name="Table 6", seats=2
        )


@pytest.fixture
def steak(restaurant, tax_rate_a):
    with tenant_context(restaurant.id):
        return Item.objects.create(
            tenant=restaurant,
            sku="STEAK",
            name="Sirloin steak",
            price_cents=120000,
            unit=UnitOfMeasure.EACH,
            tax_rate=tax_rate_a,
            track_stock=False,
        )


@pytest.fixture
def soda(restaurant, tax_rate_a):
    with tenant_context(restaurant.id):
        return Item.objects.create(
            tenant=restaurant,
            sku="SODA",
            name="Soda",
            price_cents=15000,
            unit=UnitOfMeasure.EACH,
            tax_rate=tax_rate_a,
            track_stock=False,
        )


@pytest.fixture
def chilli_item(restaurant, tax_rate_a):
    """The catalogue item a priced modifier bills as.

    A priced modifier has to be sellable, because ``create_sale`` prices from
    the catalogue and ignores anything a client claims - see ``bill_order``.
    """
    with tenant_context(restaurant.id):
        return Item.objects.create(
            tenant=restaurant,
            sku="EXTRA-CHILLI",
            name="Extra chilli",
            price_cents=2000,
            unit=UnitOfMeasure.EACH,
            tax_rate=tax_rate_a,
            track_stock=False,
        )


@pytest.fixture
def rare(doneness, restaurant):
    """Resolved inside a tenant binding.

    Reading ``doneness.modifiers`` from a test body would run outside one, and
    RLS correctly returns nothing - which surfaces as DoesNotExist rather than
    as anything about isolation. Resolving here keeps the tests about
    restaurants.
    """
    with tenant_context(restaurant.id):
        return doneness.modifiers.get(name="Rare")


@pytest.fixture
def medium(doneness, restaurant):
    with tenant_context(restaurant.id):
        return doneness.modifiers.get(name="Medium")


@pytest.fixture
def no_onions(extras, restaurant):
    with tenant_context(restaurant.id):
        return extras.modifiers.get(name="No onions")


@pytest.fixture
def chilli(extras, restaurant):
    with tenant_context(restaurant.id):
        return extras.modifiers.get(name="Extra chilli")


@pytest.fixture
def doneness(restaurant, steak):
    """Exactly one choice, required. A steak with no doneness cannot be cooked."""
    with tenant_context(restaurant.id):
        group = ModifierGroup.objects.create(
            tenant=restaurant, name="How would you like it", min_choices=1, max_choices=1
        )
        group.items.add(steak)
        for name in ("Rare", "Medium", "Well done"):
            Modifier.objects.create(tenant=restaurant, group=group, name=name)
        return group


@pytest.fixture
def extras(restaurant, steak, chilli_item):
    """Any number, one of them priced."""
    with tenant_context(restaurant.id):
        group = ModifierGroup.objects.create(
            tenant=restaurant, name="Extras", min_choices=0, max_choices=0
        )
        group.items.add(steak)
        Modifier.objects.create(tenant=restaurant, group=group, name="No onions")
        Modifier.objects.create(
            tenant=restaurant,
            group=group,
            name="Extra chilli",
            price_cents=2000,
            item=chilli_item,
        )
        return group
