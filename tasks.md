# Tasks

Living checklist, grouped by milestone. Items are checked off as they are
finished, not in batches at the end. New tasks are added here the moment they
are discovered rather than done silently.

**Legend** — `[x]` done · `[ ]` outstanding · *(discovered)* added mid-build
rather than planned up front.

---

## Milestone 1 — Tenant and auth foundation · [#1](https://github.com/garisonmike/pos/issues/1)

### Infrastructure
- [x] Repository skeleton, `.gitignore`, `.env.example`
- [x] Docker Compose: Postgres 18 and the API, one `docker compose up`
- [x] Postgres init script creating a `NOSUPERUSER NOBYPASSRLS` application role
- [x] Entrypoint that migrates and seeds a platform admin on every start
- [x] Settings split into base / dev / test / prod
- [x] `pyproject.toml` with pinned dependencies, pytest and ruff configuration

### Tenant isolation
- [x] `TenantOwnedModel` abstract base and `TenantManager`
- [x] Row-Level Security policy helper for use from migrations
- [x] `FORCE ROW LEVEL SECURITY` on every tenant table *(discovered: plain `ENABLE` exempts the table owner, and Django's migration role owns every table, so the policies would have been inert)*
- [x] `tenant_context()` and `bypass_rls()` context managers
- [x] `TenantBindingMiddleware`, binding by URL prefix
- [x] Guard rail: binding outside a transaction raises rather than silently doing nothing
- [x] Cleanup that survives a failed transaction *(discovered: restoring the database setting runs a query, which itself fails once the transaction is broken — leaving the in-process binding set and later work writing rows against a stale tenant)*
- [x] Tenant status cache with immediate invalidation on suspension
- [x] Audit trail distinguishes "no tenant supplied" from an explicit platform-level entry *(discovered while chasing the above)*

### Data model
- [x] `Tenant` with business type, status, VAT mode, branding, KRA PIN
- [x] `TenantModule` with per-module config
- [x] `Store`, with the multi-branch seam in place
- [x] `User` with Owner / Manager / Cashier and a platform-admin flag
- [x] Per-business username uniqueness *(discovered: needs `auth.E003` silenced and a custom `get_by_natural_key`, both documented where they sit)*
- [x] `Device` for registered tills
- [x] `Category`, `TaxRate`, `Item`, `Barcode`
- [x] `AuditLog`
- [x] Migrations with RLS policies applied

### Authentication
- [x] JWT with a tenant claim on both access and refresh tokens
- [x] Tenant sign-in (business slug, username, password)
- [x] PIN sign-in bound to a registered device
- [x] Platform operator sign-in on a separate route
- [x] Token refresh preserving the tenant claim
- [x] Tenant-aware refresh view *(discovered: a refresh request carries its token in the body, not the Authorization header, so the middleware bound no tenant and the user lookup was refused by isolation — an expired till could never refresh)*
- [x] Separate refresh route for platform operators, who have no tenant to bind
- [x] Sign-out with refresh token blacklisting
- [x] `/auth/me/`, change own password
- [x] Role permission classes built on an explicit role ordering

### Tenant self-service
- [x] Read and update own settings and receipt branding
- [x] List enabled modules
- [x] Business-type templates endpoint
- [x] Setup wizard, guarded to run exactly once

### Platform administration
- [x] Onboard a business with its owner account
- [x] Suspend and reactivate
- [x] Per-tenant usage counts for invoicing
- [x] Read a business's staff for support
- [x] Hardened Django admin at a configurable path, `is_platform_admin` only
- [x] `ensure_platform_admin` management command, idempotent

### Tests
- [x] RLS coverage: every tenant table has a policy and forces it
- [x] The connected database role is neither superuser nor `BYPASSRLS`
- [x] No `FloatField` anywhere in the schema
- [x] Cross-tenant ORM access returns nothing
- [x] Cross-tenant API access returns 404, not 403
- [x] Tenant binding does not survive its transaction
- [x] Every platform route requires a platform administrator, checked by walking the URL conf
- [x] Money rounding edge cases, both tax directions, cash rounding
- [x] Sign-in, PIN sign-in, refusals, token claims
- [x] Role boundaries for cashier, manager and owner
- [x] Setup wizard, including the second-run guard
- [x] Audit trail written and free of credentials

### Infrastructure fixes found while building
- [x] Postgres 18 volume mount moved to `/var/lib/postgresql` *(discovered: the 18 images store data in a major-version subdirectory and refuse to start when the old `/data` path is mounted)*
- [x] API container runs as the host user, so generated migrations are not owned by root
- [x] Platform router uses `SimpleRouter` *(discovered: `DefaultRouter`'s API root view carries no permission classes, and behind the platform prefix isolation is lifted — caught by the URL-conf walking test)*
- [x] Business-type template reapplies on setup *(discovered: it only created missing module rows, so an owner changing business type in the wizard did not get that type's modules switched on)*
- [x] Schema introspection guard on tenant-scoped querysets, so path parameter types are derived correctly

### Documentation
- [x] `README.md` — setup from clean machine, tests, environment variables
- [x] `ARCHITECTURE.md` — design, isolation strategy, offline-sync design
- [x] `drf-spectacular` schema and Swagger UI
- [x] Docstrings on every model, serializer, view and non-trivial function
- [x] `CHANGELOG.md`
- [x] `tasks.md` and `progress.md`

---

## Milestone 2 — Catalog, inventory, first Flutter screens · [#2](https://github.com/garisonmike/pos/issues/2)

### Carried over from milestone 1's sign-off
- [x] Redis in the compose file, serving the tenant status cache and lockout
- [x] PIN lockout: 5 failures on a device refuses it for 15 minutes, `429` with `retry_after_seconds`
- [x] A second counter per user, so attempts spread across tills still run out
- [x] Unregistered device tokens deliberately excluded from the count, so nobody can lock a till they cannot touch
- [x] Every failed attempt written to the audit trail, filed against the username and never the user
- [x] Scoped request throttles on both sign-in routes
- [x] `pos.E001`/`E002` boot checks on `PLATFORM_ADMIN_URL`, `pos.E003` on `SECRET_KEY`, `pos.W001` on a per-process cache
- [x] Cross-tenant read boundary proved in both directions in one file
- [x] ARCHITECTURE: how the platform reads across businesses, and the migration/backfill escape hatch
- [x] **RLS gap closed on five tables owned indirectly** *(discovered: the milestone 1 coverage test only looked for a direct `tenant` column. `token_blacklist_outstandingtoken` stores the encoded refresh token itself against a user id, so a tenant-scoped query could have read another business's refresh tokens verbatim. Also `token_blacklist_blacklistedtoken`, `accounts_user_groups`, `accounts_user_user_permissions`, `django_admin_log`.)*
- [x] Coverage test widened to resolve ownership transitively over foreign keys, including Django's auto-created join tables

### Catalog
- [x] `Item` extensions: `is_price_variable`, `is_available`, `short_name`, `image`, `sort_order`
- [x] Category CRUD, scoped per business
- [x] TaxRate CRUD, one default per business enforced *(discovered: the old default was stood down after saving, so the insert hit the partial unique index and returned a conflict)*
- [x] Item CRUD covering products and services
- [x] Several barcodes per item, any of which resolves to the item
- [x] Search and barcode lookup tuned for the till
- [x] Mixed inclusive/exclusive tax in one catalogue, rounding edges tested
- [x] Categories and tax rates refuse deletion while referenced, with a usable message

### Inventory
- [x] `StockItem` per item per store, with `reorder_level`
- [x] `StockMovement` append-only ledger with `balance_after`
- [x] Adjustments requiring a reason, manager and above, audited
- [x] Adjust by delta or by counted total
- [x] Row lock in `apply_movement` so concurrent sales cannot lose a movement
- [x] Negative stock allowed and surfaced rather than refused
- [x] Low-stock endpoint, and a per-item ledger endpoint
- [x] `rebuild_quantity` so the cached total can always be re-derived

### CSV import
- [x] Validate then commit, per-row errors rather than all-or-nothing
- [x] Upsert by SKU, several barcodes per row
- [x] Unknown categories created and reported; unknown tax rates rejected per row
- [x] Validate token expires after an hour
- [x] Token tied to a hash of the file, so commit cannot swap it
- [x] Commit re-checks every referenced category and tax rate, failing changed rows individually
- [x] Opening stock arrives through the ledger
- [x] Downloadable template with a product and a service example

### Flutter
- [x] Scaffold: Riverpod, dio, secure storage
- [x] Business ID entry, device registration, password and PIN sign-in
- [x] Token refresh in an interceptor, with a single in-flight guard
- [x] Read-only catalogue browse, category filter, search, barcode lookup
- [x] Item detail sheet
- [x] 21 widget and unit tests, `flutter analyze` clean

### Close
- [x] Isolation tests on every new table, to milestone 1's bar
- [x] Retail template proven end to end
- [x] Docs, changelog, issue closed with a summary

---

### Deferred out of milestone 2
- [ ] Stock take / recount flow *(the adjustment path with reason COUNT covers the immediate need; a guided full recount is its own screen)*
- [ ] Camera barcode scanning *(typed and hardware-scanner entry works; most Kenyan counters use a scanner that behaves as a keyboard)*
- [ ] Background job for imports over 5,000 rows *(refused with a clear message for now, rather than carrying a worker nothing needs yet)*
- [ ] Per-branch price overrides *(seam left open: resolve as `override ?? item.price`)*

## Milestone 3 — Sales, payments, receipts and offline sync · [#3](https://github.com/garisonmike/pos/issues/3)

### Groundwork
- [x] GitHub issue with scope and acceptance criteria
- [x] Audit redaction fixed before any credential model existed *(discovered: `REDACTED_FIELDS` matched whole keys, so `consumer_secret` and `passkey` would have been written to a manager-readable table in clear)*
- [x] Redaction now substring-matched and recursive into nested dicts and lists

### Cart arithmetic — `apps/sales/pricing.py`
- [x] Line extension with fractional quantities for goods sold by weight
- [x] Line discounts, percentage and fixed amount, both applying together
- [x] Whole-cart discounts
- [x] Cart discount apportioned across lines **before** tax, since each line carries its own rate
- [x] Largest-remainder apportionment, deterministic, parts always summing to the whole
- [x] Tax per line against that line's own rate and `is_inclusive`
- [x] Mixed inclusive and exclusive lines totalling correctly on one sale
- [x] Round per line then sum, never round a total
- [x] Cash tender and change
- [x] 241 tests, apportionment property walked from one cent upward across five weightings

### Sale state machine — `apps/sales/states.py`
- [x] `OPEN`, `AWAITING_PAYMENT`, `PAID`, `PARTIALLY_REFUNDED`, `REFUNDED`, `VOID`
- [x] `ALLOWED_TRANSITIONS` table, with every legal move asserted
- [x] **A paid sale cannot be voided** — asserted three ways
- [x] All 36 state pairs swept, so nothing is legal by omission
- [x] Terminal states are genuinely terminal
- [x] `LedgerPosition`: outstanding, overpaid, refundable
- [x] `derive_state` separated from `ALLOWED_TRANSITIONS` *(discovered: one table was doing two jobs — the table governs what a person may do, deriving governs what the ledgers say, and a stale cache must be able to catch up across more than one edge)*
- [x] `VOID` is sticky against every ledger, which the callback guard relies on
- [x] `CREDITABLE_STATES` is `AWAITING_PAYMENT` alone

### Data model
- [x] `Sale` with client uuid, receipt number, provisional reference, cached state
- [x] `SaleLine` snapshotting name, price, discounts, tax rate **and** `is_inclusive`
- [x] `Payment`, append-only, split payments as several rows
- [x] `Refund` and `RefundLine`, append-only, with a per-line restock flag
- [x] `SaleDiscrepancy` for totals mismatch, negative stock, overpayment, late payment
- [x] `ReceiptCounter` *(discovered: it lived outside `models.py`, so Django never discovered it and `makemigrations` reported no changes)*
- [x] `MpesaCredential`, Fernet-encrypted, key separate from `SECRET_KEY`
- [x] `PaymentIntent` with client uuid, checkout request id and callback token
- [x] `MpesaCallback` recording every callback including refusals
- [x] Migrations with RLS on all ten money tables
- [x] `payment_intent` policy moved to `0002` *(discovered: sales and payments reference each other, so Django defers those FKs **and the tenant column with them**)*

### Sale services — `apps/sales/services.py`
- [x] `create_sale` pricing from the catalogue
- [x] `recompute_state` as the single writer of `Sale.state`
- [x] `ledger_position` summing the rows rather than trusting a running total
- [x] `take_cash` through the same ledger and state machine as M-Pesa
- [x] Cash rounding to the shilling, difference recorded on the sale
- [x] Receipt number allocated under a row lock inside the sale's transaction
- [x] Stock moved on settlement, negative surfaced as a discrepancy
- [x] `void_sale`, reason mandatory
- [x] `refund_sale` at the price actually charged, including discount share
- [x] Refund restock, honouring the per-line flag

### Guards
- [x] Client-supplied prices ignored on non-variable items
- [x] Variable price floor: `price_cents` is a minimum, not a suggestion *(decided this milestone; going below is a discount, which leaves a trail)*
- [x] Void checks the ledger independently of the cached state, proved by forcing the cache to disagree
- [x] Items from another business refused at the service layer
- [x] Unavailable items cannot be rung up
- [x] 19 negative-path tests

### Outstanding
- [ ] Manager authorization for discounts *(discovered: `create_sale` accepts discounts with no authority check at all; unreachable until the endpoint exists, live the moment it does)*
- [ ] Cash checkout API end to end
- [ ] Split payments across cash and M-Pesa
- [ ] `MpesaCredential` API, write-only
- [ ] STK push client against the Daraja sandbox
- [ ] Callback: four idempotency keys, terminal-state guard, suspect path
- [ ] Safaricom IP allowlist, mandatory on production credentials
- [ ] Reconciliation job for lapsed intents
- [ ] Unresolved suspect callbacks and discrepancies in the platform console
- [ ] Receipt PDF, branded per tenant
- [ ] ESC/POS printing from the till
- [ ] Offline sync: catalogue delta pull
- [ ] Offline sync: sale batch ingest with `client_uuid` idempotency
- [ ] Sync rejects a payload replayed with another business's token and device
- [ ] Two offline devices selling the last unit: both accepted, stock negative, flagged
- [ ] Flutter: drift queue, cart, checkout, offline indicator
- [ ] Shift open/close with cash drawer reconciliation
- [ ] SMS receipt *(stretch)*

## Milestone 4 — Compliance layer

- [ ] GitHub issue with scope and acceptance criteria
- [ ] Per-tenant invoice numbering
- [ ] Compliance adapter interface
- [ ] Manual/export adapter producing what a business would enter into eTIMS Lite
- [ ] Buyer PIN capture on a sale
- [ ] Extension point documented for a real OSCU/VSCU or gateway integration

## Milestone 5 — Reporting and analytics

- [ ] GitHub issue with scope and acceptance criteria
- [ ] Daily, weekly and monthly sales summaries
- [ ] Best sellers
- [ ] Cashier performance
- [ ] Refund rates
- [ ] CSV and PDF export
- [ ] Platform: per-tenant usage summary for invoicing
- [ ] Flutter: reports screens

## Milestone 6 — Restaurant module

- [ ] GitHub issue with scope and acceptance criteria
- [ ] Tables and table state
- [ ] Orders held against a table until payment
- [ ] Item modifiers
- [ ] Kitchen ticket printing

## Milestone 7 — Salon and services module

- [ ] GitHub issue with scope and acceptance criteria
- [ ] Service duration and staff assignment
- [ ] Appointment booking
- [ ] Staff calendar

## Milestone 8 — Pharmacy extensions

- [ ] GitHub issue with scope and acceptance criteria
- [ ] Batch and expiry tracking per stock unit
- [ ] Expiry alerts
- [ ] Batch selection at the point of sale

---

## Cross-cutting, not yet scheduled

- [ ] Flutter application scaffold *(nothing built yet; milestone 1 is backend only)*
- [ ] Rate limiting on sign-in endpoints, especially PIN sign-in *(discovered while building PIN auth: a 4-digit PIN with a valid device token is brute-forceable without throttling)*
- [ ] CI pipeline running the test suite on every push
- [ ] Production deployment: Redis for the tenant status cache, static and media serving, TLS
- [ ] Backup and restore procedure, including a documented per-tenant export
- [ ] Structured request logging with the tenant attached
