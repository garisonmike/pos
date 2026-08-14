# Changelog

All notable changes to this project, newest first. Updated at the end of each
milestone.

---

## [0.3.0] — 2026-08-14 — Milestone 3: selling, payments, receipts, offline and the drawer

A shop can now trade. Ring up a sale, take cash or M-Pesa, hand over a receipt,
keep selling when the network drops, and count the drawer at the end of the day.

### Selling

- Cart arithmetic in integer cents throughout: discount before tax, a whole-cart
  discount apportioned across lines before tax by largest remainder, round per
  line then sum. Mixed inclusive and exclusive tax rates total correctly on one
  sale.
- Sale state machine with every transition asserted and all 36 state pairs swept,
  so nothing is legal by omission. **A paid sale cannot be voided.**
- Cash checkout in one request, because that is how a counter works.
- Discounts need authority. A manager or owner authorises from their own
  session; a cashier needs a manager's credential in the request, verified and
  discarded. Refusals are audited against the username typed, with no user
  foreign key — nobody proved they were that person.

### M-Pesa

- STK push against Daraja, with per-tenant credentials encrypted at rest under a
  key separate from `SECRET_KEY`.
- Four idempotency keys, one guarded settlement path, and a terminal-state guard
  so a callback can only credit a sale still awaiting payment.
- Safaricom IP allowlist, mandatory once a tenant runs on production credentials.
  `X-Forwarded-For` is read from the **last** entry — the leftmost is
  attacker-controlled.
- A reconciliation job for pushes whose callback never arrived, and a backfill
  that replaces the placeholder reference when a late callback finally brings
  the real receipt code.

### Receipts

- Sequential per-tenant numbering, allocated under a row lock inside the sale's
  own transaction.
- Text for a 58mm thermal printer and PDF for everything else, both from one
  source so they cannot disagree about what was sold.
- ESC/POS printing from the till. A printer out of paper never fails a sale.

### Offline

- The till sells with no connection, queueing to its own database.
- Batch ingest with a verdict per sale: one bad sale in a batch of forty does
  not strand the other thirty-nine.
- Idempotent replay by database constraint rather than by a lookup — the obvious
  look-then-create shape is a race, and two threads uploading the same batch
  would both insert. Proved with real threads, two and five at once.
- Offline PIN lockout in the till's own database, since the server's counter
  lives in Redis and Redis is exactly what is unreachable. Refused attempts sync
  home as audit entries.
- A `pin_version` fingerprint catches a till approving a discount against a PIN
  that has since been changed or revoked.
- A till that undercharged because a price rose while it was offline settles for
  what it collected, with the shortfall recorded and flagged.

### Shifts and the cash drawer

- Open a drawer with a counted float, record cash in and out, close it with a
  count.
- **The count is blind.** No endpoint reports what an open drawer is expected to
  hold; the expectation is computed only once the cashier's figure is in hand.
- Cash only. M-Pesa never enters the expected-cash figure.
- Closing figures are frozen. A sale arriving after the drawer closed raises a
  `LATE_ATTRIBUTION` discrepancy rather than rewriting what somebody signed off.

### Fixed

- **Any cash sale whose total was not a whole shilling took the customer's money
  and never settled.** `ledger_position` read the raw total while `take_cash`
  charged the rounded figure, so the two disagreed by up to fifty cents: no
  receipt number, no stock movement, sale left open. With VAT-inclusive pricing
  that is most sales. Settlement is now decided against
  `collectable_cents = total + rounding - written_off`.
- Audit redaction matched whole field names, so `consumer_secret` and `passkey`
  would have been written to a manager-readable table in clear. Fixed before the
  credential model existed.
- `SaleViewSet.void` caught every exception and reported it as bad input, hiding
  genuine bugs behind a 400.

### Known limitations

- **A stolen till is a stolen manager PIN.** The catalogue download sends a
  manager's PIN hash so an offline discount check has something to verify
  against. Four to six digits is brute-forceable by anyone holding the tablet,
  and the same PIN signs in. Tracked under account management: a separate
  offline approval code, revocable on its own.
- The `pin_version` fingerprint catches staleness and revocation. It does not
  prove the device performed the check — anyone who can edit the till's database
  can read the version it holds.
- A shift's total and the sales total will not tie out without reading both.
  Deliberate; every discrepancy carries a foreign key to its shift so the join
  is a query when reporting arrives.
- **Shifts are server-side only.** The models, the reconciliation and the API
  are built and tested, but there is no cashier screen for opening or closing a
  drawer - a shop would have to drive it through the API. The till screens for
  it are the first thing outstanding from this milestone.

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
