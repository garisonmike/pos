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

## Milestone 2 — Generic catalog and inventory

- [ ] GitHub issue with scope and acceptance criteria
- [ ] Category CRUD
- [ ] Tax rate CRUD, enforcing one default per business
- [ ] Item CRUD covering both products and services
- [ ] Barcode management, several per item
- [ ] Item search and barcode lookup endpoints, tuned for the till
- [ ] Item images
- [ ] `StockItem` per item per store
- [ ] `StockMovement` append-only ledger
- [ ] Stock adjustments, manager and above, reason required
- [ ] Low-stock thresholds and an alerts endpoint
- [ ] Stock take / recount flow
- [ ] Bulk item import from CSV
- [ ] Retail template working end to end before any other business type
- [ ] Tests: stock ledger arithmetic, negative-stock handling, isolation on every new table

## Milestone 3 — Sales and checkout

- [ ] GitHub issue with scope and acceptance criteria
- [ ] `Sale`, `SaleLine`, `Payment`, `Refund` models
- [ ] Cart building, barcode and search lookup
- [ ] Line-item and cart-level discounts
- [ ] Tax calculation respecting per-item `is_inclusive`
- [ ] Cash payment with shilling rounding recorded
- [ ] M-Pesa STK Push via Daraja, with callback handling
- [ ] Split payments
- [ ] Voids and partial refunds, manager and above, audited
- [ ] Receipt generation: printable and PDF, branded per tenant
- [ ] SMS receipt *(stretch)*
- [ ] Offline sync: catalogue delta pull
- [ ] Offline sync: sale batch ingest with `client_uuid` idempotency
- [ ] `StockDiscrepancy` for offline sales that drive stock negative
- [ ] Device registration and sale attribution
- [ ] Shift open/close with cash drawer reconciliation
- [ ] Flutter: cart, checkout, payment, receipt screens
- [ ] Flutter: SQLite outbox and sync engine
- [ ] Tests: idempotent replay, price snapshotting, offline conflict handling

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
