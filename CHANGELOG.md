# Changelog

All notable changes to this project, newest first. Updated at the end of each
milestone.

---

## [0.2.0] — 2026-08-13 — Milestone 2: catalogue, stock and the first till screens

What a business sells and how much of it is on the shelf, plus an Android app
that signs in and browses. No checkout yet — that is milestone 3.

### Security

- **Closed an isolation gap on five tables owned through a foreign key.**
  Milestone 1's coverage test looked for a `tenant` *column*; these belong to a
  business by way of a relation, and four of them are created by Django rather
  than declared here, so a walk over our own models never saw them. Four were
  harmless. `token_blacklist_outstandingtoken` was not: it stores the encoded
  refresh token in a text column beside the user it was issued to, so a query
  made while one business was bound could have read another's refresh tokens
  verbatim.
- Coverage test now resolves ownership transitively over foreign keys and
  includes auto-created models, so this class of table cannot slip through
  again.
- Till lockout: five consecutive wrong PINs refuse that device for fifteen
  minutes regardless of pacing, with a second counter per user so attempts
  spread across tills still run out. Every failure goes to the audit trail.
- Startup checks refuse to run with the published platform console path or the
  development signing key, and warn about a per-process cache.

### Added

**Catalogue**
- `Item` extended with `short_name`, `is_price_variable`, `is_available`,
  `image` and `sort_order`.
- Category, tax rate, item and barcode CRUD, scoped per business.
- Several barcodes per item; any of them resolves to the item.
- Search across name, short name, SKU and barcode, and a trimmed till endpoint.
- Per-item tax breakdown, so mixed inclusive and exclusive pricing is visible
  in the API rather than implied.

**Stock**
- `StockItem` per item per store, with a per-branch reorder level.
- `StockMovement`, append-only, with the balance after each entry.
- Adjustments by delta or counted total, requiring a reason, audited.
- Low-stock endpoint, and a per-item ledger.

**Bulk import**
- Two-phase CSV import with per-row errors rather than all-or-nothing failure.
- Upsert by SKU, several barcodes per row, opening stock through the ledger.
- Unknown categories created and reported; unknown tax rates rejected per row.
- Token tied to a hash of the file, expiring after an hour, and references
  re-resolved at commit so anything renamed in between fails only its own row.
- Downloadable template with a product and a service example.

**Till application**
- Flutter scaffold: Riverpod, dio, go_router, secure storage.
- Business ID entry, device registration, password and PIN sign-in.
- Read-only catalogue browse with category filter, search and barcode lookup.
- 21 widget and unit tests.

**Infrastructure**
- Redis, serving the tenant status cache and lockout counters.
- Scoped request throttles on both sign-in routes.

### Fixed

- Marking a new default tax rate stood the old one down *after* saving, so the
  insert hit the partial unique index and returned a conflict for something the
  caller was entitled to do.

### Known gaps

- No checkout, payments or offline queue yet.
- No CI pipeline.
- Camera barcode scanning is typed entry for now; most Kenyan counters use a
  scanner that behaves as a keyboard.

---

## [0.1.0] — 2026-08-13 — Milestone 1: tenant and auth foundation

The foundation the rest of the platform sits on. Multiple independent
businesses can be onboarded onto one deployment, each with isolated data, its
own staff and roles, and its own settings.

### Added

**Tenant isolation**
- Shared-schema multi-tenancy with a `tenant_id` on every tenant-owned table.
- Postgres Row-Level Security on every one of those tables, with
  `FORCE ROW LEVEL SECURITY` so policies apply to the table owner as well.
- An application database role created `NOSUPERUSER` and `NOBYPASSRLS`, so the
  policies cannot be bypassed.
- `TenantBindingMiddleware`, binding the tenant from the access token for the
  life of one transaction.
- `tenant_context()` and `bypass_rls()`, the latter reachable only from the
  platform surfaces.
- `TenantOwnedModel` and `TenantManager` as the application-level scoping layer.

**Businesses**
- `Tenant` with business type, lifecycle status, VAT mode, receipt branding and
  KRA PIN.
- `TenantModule` for optional capabilities, with per-module configuration.
- Business-type templates for retail, restaurant, salon and pharmacy, applied
  as defaults at setup.
- A setup wizard that creates the first branch, default tax rate and staff, and
  runs exactly once.

**Accounts**
- Custom user model with Owner, Manager and Cashier roles, unique per business.
- JWT authentication carrying the tenant, with refresh and blacklisting.
- Device-bound PIN sign-in for fast cashier switching at the till.
- A separate sign-in route for the platform operator.
- Registered devices with one-time tokens, stored hashed.

**Catalogue and branches**
- `Store`, with the multi-branch path open from the start.
- `Item` covering both physical products and services, `Category`, `TaxRate`
  with a per-rate `is_inclusive` flag, and `Barcode` allowing several per item.
  Tables and policies only; management endpoints arrive in milestone 2.

**Platform administration**
- Onboard, suspend and reactivate businesses.
- Per-tenant usage counts for invoicing.
- A hardened Django admin at a configurable path, restricted to platform
  administrators rather than merely to staff.
- An idempotent `ensure_platform_admin` command run on every container start.

**Money**
- Integer-cent arithmetic with half-up rounding, tax rates in basis points, and
  inclusive-tax splitting that guarantees `net + tax == gross`.
- Cash rounding to the shilling.

**Auditing**
- `AuditLog` recording actor, action, entity, reason and a redacted before/after.
- Written explicitly rather than by signals, so every entry has an actor and a
  reason.

**Infrastructure and documentation**
- `docker compose up` as the single documented setup step.
- OpenAPI schema and Swagger UI generated from the code.
- README, ARCHITECTURE, tasks and progress log.

### Tests

- Structural: every tenant table has an isolation policy and forces it; the
  connected role can neither bypass RLS nor act as superuser; no float fields
  exist anywhere in the schema.
- Isolation: cross-tenant reads return nothing, cross-tenant writes are
  refused, an unbound request sees nothing, and a tenant binding does not
  survive its transaction.
- End to end: cross-tenant API access returns 404 rather than 403, payloads
  cannot smuggle in another business's identifier, and every route under the
  platform prefix requires a platform administrator.
- Money: rounding edges in both tax directions, discounts, and cash rounding.
- Authentication, role boundaries, the setup wizard and the audit trail.

### Fixed during the milestone

- Token refresh returned a server error whenever it was used the way a real
  client uses it. The refresh token travels in the request body, so no tenant
  was bound and the user lookup was refused by isolation. Refresh now reads the
  tenant from the token's own claim.
- A failed query inside a tenant-bound block left the tenant bound in-process,
  because the cleanup that restores it ran a query on the already-aborted
  transaction and raised. Subsequent work could then write rows against a
  business that no longer existed.
- Changing business type in the setup wizard did not switch on that type's
  modules.
- The platform API exposed a router root view that declared no permissions,
  behind the one prefix where tenant isolation is lifted.

### Known gaps

- No rate limiting on sign-in endpoints yet.
- No Flutter client yet; this milestone is backend only.
- No CI pipeline yet.
