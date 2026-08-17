"""Generate the cross-implementation pricing fixture.

The till prices carts a second time, in Dart, because a cashier cannot be told
"no total until the network comes back". Two implementations of the same
arithmetic will drift unless something forces them together, so this dumps a
spread of carts priced by the *server* and the Dart test asserts against it.

Regenerate with:
    docker compose run --rm api python gen_pricing_fixture.py
"""

import json
import random
from decimal import Decimal

from apps.sales.pricing import LineInput, price_cart

random.seed(20260814)

RATES = [0, 800, 1600, 1600, 2500]
QUANTITIES = ["1", "2", "0.5", "2.5", "0.333", "1.125", "7", "0.001"]

cases = []
for _case_index in range(120):
    line_count = random.randint(1, 5)
    lines = []
    for line_index in range(line_count):
        rate = random.choice(RATES)
        lines.append(
            LineInput(
                item_id=f"item-{line_index}",
                name=f"Item {line_index}",
                sku=f"SKU{line_index}",
                unit="EACH",
                unit_price_cents=random.randint(1, 500_000),
                quantity=Decimal(random.choice(QUANTITIES)),
                tax_rate_bps=rate,
                tax_is_inclusive=random.choice([True, False]),
                discount_bps=random.choice([0, 0, 0, 500, 1000, 3333]),
                discount_cents=random.choice([0, 0, 0, 100, 5000]),
            )
        )

    cart_bps = random.choice([0, 0, 0, 250, 1000, 5000])
    cart_cents = random.choice([0, 0, 0, 1000, 25000])

    totals = price_cart(
        lines, cart_discount_bps=cart_bps, cart_discount_cents=cart_cents
    )

    cases.append(
        {
            "lines": [
                {
                    "item_id": line.item_id,
                    "name": line.name,
                    "unit_price_cents": line.unit_price_cents,
                    "quantity_milli": int(line.quantity * 1000),
                    "tax_rate_bps": line.tax_rate_bps,
                    "tax_is_inclusive": line.tax_is_inclusive,
                    "discount_bps": line.discount_bps,
                    "discount_cents": line.discount_cents,
                }
                for line in lines
            ],
            "cart_discount_bps": cart_bps,
            "cart_discount_cents": cart_cents,
            "expected": {
                "subtotal_cents": totals.subtotal_cents,
                "discount_cents": totals.discount_cents,
                "tax_cents": totals.tax_cents,
                "total_cents": totals.total_cents,
                "lines": [
                    {
                        "gross_before_discount_cents": line.gross_before_discount_cents,
                        "line_discount_cents": line.line_discount_cents,
                        "cart_discount_share_cents": line.cart_discount_share_cents,
                        "net_cents": line.net_cents,
                        "tax_cents": line.tax_cents,
                        "gross_cents": line.gross_cents,
                    }
                    for line in totals.lines
                ],
            },
        }
    )

with open("pricing_cases.json", "w") as handle:
    json.dump({"generated_by": "backend/gen_pricing_fixture.py", "cases": cases}, handle, indent=1)

print(f"wrote {len(cases)} cases")
