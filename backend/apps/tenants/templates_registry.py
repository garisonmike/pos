"""
Business-type templates.

A template is nothing more than a set of defaults: which modules start switched
on, and how tax is presented. It is applied once, when the owner finishes the
setup wizard, and has no effect afterwards. Everything it sets remains editable.

Keeping templates as plain data rather than as subclasses or separate code
paths is the mechanism that stops the four supported business types from
becoming four forks of the product. A pharmacy differs from a duka by one
enabled module, not by a different sales pipeline.

The module named in this file is ``templates_registry`` rather than
``templates`` so that it is not mistaken for a Django template directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.tenants.models import BusinessType, ModuleKey, VatMode


@dataclass(frozen=True)
class BusinessTemplate:
    """Defaults applied to a new business of a given type."""

    business_type: str
    label: str
    description: str
    enabled_modules: tuple[str, ...]
    default_vat_mode: str = VatMode.INCLUSIVE
    #: Tax rate created during setup. 16% is the standard Kenyan VAT rate;
    #: zero-rated and exempt goods get additional rates added by the owner.
    default_tax_rate_bps: int = 1600
    default_tax_rate_name: str = "VAT 16%"
    tracks_stock_by_default: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


#: Every business gets these. M-Pesa and compliance are universal in Kenya, and
#: a business with no stock tracking still needs both.
_BASELINE_MODULES: tuple[str, ...] = (ModuleKey.MPESA, ModuleKey.COMPLIANCE)


BUSINESS_TEMPLATES: dict[str, BusinessTemplate] = {
    BusinessType.RETAIL: BusinessTemplate(
        business_type=BusinessType.RETAIL,
        label="Retail shop",
        description=(
            "Barcode or search checkout with stock tracking. The default, and "
            "the shape a duka, hardware shop or small supermarket needs."
        ),
        enabled_modules=(*_BASELINE_MODULES, ModuleKey.STOCK),
    ),
    BusinessType.RESTAURANT: BusinessTemplate(
        business_type=BusinessType.RESTAURANT,
        label="Restaurant or bar",
        description=(
            "Orders held against a table until payment, item modifiers, and "
            "kitchen tickets. Stock is tracked for drinks and packaged goods."
        ),
        enabled_modules=(*_BASELINE_MODULES, ModuleKey.STOCK, ModuleKey.RESTAURANT),
        notes=(
            "Prepared food is normally sold without stock tracking while "
            "bottled drinks are tracked, so items decide this individually.",
        ),
    ),
    BusinessType.SALON: BusinessTemplate(
        business_type=BusinessType.SALON,
        label="Salon or services",
        description=(
            "Services with a duration and an assigned staff member, booked in "
            "advance. Retail stock is off by default and can be switched on."
        ),
        enabled_modules=(*_BASELINE_MODULES, ModuleKey.APPOINTMENTS),
        tracks_stock_by_default=False,
    ),
    BusinessType.PHARMACY: BusinessTemplate(
        business_type=BusinessType.PHARMACY,
        label="Pharmacy",
        description=(
            "Everything a retail shop has, plus batch numbers and expiry dates "
            "held per stock unit, with alerts before stock expires."
        ),
        enabled_modules=(
            *_BASELINE_MODULES,
            ModuleKey.STOCK,
            ModuleKey.PHARMACY_BATCHES,
        ),
    ),
}


def get_template(business_type: str) -> BusinessTemplate:
    """Look up a template, falling back to retail.

    Retail is the fallback because it is the most constrained sensible default:
    it enables stock tracking, which a business can turn off, rather than
    leaving it off and letting a shop sell untracked inventory unnoticed.
    """
    return BUSINESS_TEMPLATES.get(business_type, BUSINESS_TEMPLATES[BusinessType.RETAIL])


def module_defaults(business_type: str) -> dict[str, bool]:
    """Return every module key mapped to whether this business type starts with it on.

    All keys are returned, not only the enabled ones, so that a tenant always
    has a complete set of module rows. A missing row and a disabled row would
    otherwise be different states meaning the same thing.
    """
    enabled = set(get_template(business_type).enabled_modules)
    return {key: key in enabled for key in ModuleKey.values}
