# Architecture

This document explains how the system is built and, more importantly, why.
Reading it end to end should take about twenty minutes and leave you able to
find your way around the codebase.

If you read only one section, read **[Tenant isolation](#tenant-isolation)**.
It is the decision everything else is arranged around.

---

## Contents

1. [What this system is](#what-this-system-is)
2. [The shape of it](#the-shape-of-it)
3. [Tenant isolation](#tenant-isolation)
4. [The generic catalog](#the-generic-catalog)
5. [Modules, not forks](#modules-not-forks)
6. [Authentication](#authentication)
7. [Money](#money)
8. [Offline sync](#offline-sync)
9. [M-Pesa](#m-pesa)
10. [Auditing](#auditing)
11. [Technology choices](#technology-choices)
12. [What is deliberately not here](#what-is-deliberately-not-here)

---

## What this system is

One deployment, many independent businesses. A shop in Nakuru and a salon in
Mombasa both use the same running instance, see completely separate data, and
have their own staff, prices, receipts and enabled features.

The commercial model shapes the engineering. Because the customers are small
businesses paying small amounts, the per-tenant operational cost has to be
close to zero — that rules out a database or a deployment per customer. And
because the product is sold to strangers rather than run for one owner, a
cross-tenant data leak is not an embarrassment, it is the end of the business.
Those two constraints pull in opposite directions, and reconciling them is the
central problem this architecture solves.

---

## The shape of it

```
   ┌──────────────────────────┐
   │   Flutter till (Android) │
   │  ┌────────────────────┐  │
   │  │ SQLite: catalogue  │  │   works with no network
   │  │ SQLite: outbox     │  │
   │  └────────────────────┘  │
   └───────────┬──────────────┘
               │ HTTPS, JWT bearing a tenant claim
               ▼
   ┌──────────────────────────────────────────────┐
   │ Django + DRF                                 │
   │                                              │
   │  TenantBindingMiddleware                     │
   │    reads the tenant from the token           │
   │    opens a transaction                       │
   │    SET LOCAL app.tenant_id = <uuid>          │
   │                                              │
   │  Views ─ permissions ─ services              │
   └───────────┬──────────────────────────────────┘
               │  as a NOSUPERUSER, NOBYPASSRLS role
               ▼
   ┌──────────────────────────────────────────────┐
   │ PostgreSQL                                   │
   │   Row-Level Security on every tenant table:  │
   │   tenant_id = current_setting('app.tenant_id')│
   └──────────────────────────────────────────────┘
               ▲
               │  Daraja STK Push callbacks (milestone 3)
   ┌───────────┴──────────────┐
   │ Safaricom M-Pesa         │
   └──────────────────────────┘
```

The request path is deliberately short. A request arrives with a JWT, the
middleware extracts the tenant from it and binds that tenant on the database
connection, and from that point on every query in the request is constrained by
the database itself.

---

## Tenant isolation

### The threat

The failure that matters is not an attacker breaking in. It is an ordinary
mistake: a developer writes `Item.objects.all()` in a new report, forgets that
it needs scoping, and one shop's report quietly includes another shop's
figures. That code passes review, passes its own tests, and works perfectly in
a single-tenant test environment.

So the design assumption is: **application code will eventually forget a
filter, and the system must survive that.**

### The choice

Three approaches were considered.

| Approach | Isolation | Ops cost | Cross-tenant reporting | Verdict |
|---|---|---|---|---|
| Database per tenant | Strongest | N migrations, N backups, N pools | Painful | Ops cost per tenant exceeds revenue per tenant. Rejected. |
| Schema per tenant | Strong | Migrations run per schema — minutes now, hours at scale | Must loop every schema and union | Fights the per-tenant billing view directly. Rejected. |
| Shared schema, `tenant_id` filters only | Weak | Lowest | Trivial | One forgotten filter is a breach. Rejected. |
| **Shared schema + `tenant_id` + Row-Level Security** | Strong | Lowest | Trivial | **Chosen.** |

Row-Level Security gets most of the isolation of the expensive options at the
operational cost of the cheap one. One database, one migration run, one backup,
and a billing query that is a single `GROUP BY tenant_id` — while the database
itself refuses to return another tenant's rows.

### How it works

**Layer 1 — the database.** Every table carrying a `tenant_id` has this policy:

```sql
CREATE POLICY tenant_isolation ON catalog_item
    USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        OR COALESCE(current_setting('app.bypass_rls', true), '') = 'on'
    )
    WITH CHECK (same predicate);
```

Four details are load-bearing:

- **`NULLIF(..., '')`** — an unset variable is an empty string, and casting
  that to `uuid` would raise. Mapping it to `NULL` makes the comparison false
  instead, so an unbound request sees *nothing*. The failure mode is "no data",
  never "someone else's data".
- **`current_setting(..., true)`** — the `true` is `missing_ok`; without it a
  connection that has never had the variable set raises rather than returning
  `NULL`.
- **`WITH CHECK`** — covers writes as well as reads. Without it a business
  could not *read* another's data but could still *write into* it, which is
  worse: silent corruption instead of a visible leak.
- **`FORCE ROW LEVEL SECURITY`** — this is the one that is easy to miss.
  Ordinary `ENABLE` exempts the table's owner, and Django runs migrations as
  the same role the application uses, so that role owns every table. Without
  `FORCE`, every policy in the system would be present, correct, and completely
  inert. `apps/core/tests/test_rls_coverage.py` asserts `FORCE` on every table
  precisely because this failure is invisible.

The role Django connects as is created `NOSUPERUSER NOBYPASSRLS`
(`docker/postgres/init/10-create-app-role.sh`). Superusers and `BYPASSRLS`
roles ignore every policy unconditionally, so connecting as one would disable
the entire strategy while every test still passed. A test asserts the connected
role has neither attribute.

**Layer 2 — the application.** `TenantOwnedModel` gives models their
`tenant` foreign key, and `TenantManager` scopes querysets to the tenant bound
to the current request. This is convenience and early warning, not security:
its job is to make ordinary code read naturally and to make a missing filter
show up as an obviously empty list during development.

**Layer 3 — the tests.** Three suites, run on every commit:

- **Coverage** (`test_rls_coverage.py`) walks every model with a `tenant`
  column and fails if any lacks a policy or lacks `FORCE`. A new table cannot
  ship unprotected.
- **Behaviour** (`test_tenant_isolation.py`) proves the unfiltered manager
  returns nothing across tenants, that an unbound context sees nothing, that
  cross-tenant writes are refused, and that a binding does not survive its
  transaction.
- **End to end** (`test_cross_tenant_api.py`) proves the same through the API,
  and walks the URL conf to assert every route under the platform prefix
  requires a platform administrator.

### Binding the tenant per request

`TenantBindingMiddleware` (`apps/core/middleware.py`) decides which tenant a
request may see, using **URL prefix and nothing else**:

- **Tenant mode** — everything under `/api/v1/` except the platform prefix. The
  tenant comes from the `tenant_id` claim on the access token. No token, or an
  unreadable one, means no binding, and no binding means no data.
- **Platform mode** — the console and `/api/v1/platform/`. Isolation is lifted,
  because onboarding a business and billing across all of them are genuinely
  cross-tenant jobs. Every view behind these prefixes independently requires
  `IsPlatformAdmin`.

Deciding by prefix rather than by inspecting the authenticated user avoids a
circularity: the user table is itself tenant-isolated, so working out whether
the caller may bypass isolation would need a query that isolation blocks.
Prefixes are known before any query runs. It also has a pleasant side effect —
the set of routes where isolation is lifted is something a reviewer can
enumerate by reading one URL file.

### Why the whole request is one transaction

The binding is set with `set_config('app.tenant_id', <uuid>, is_local => true)`.
The `is_local` flag scopes it to the current transaction, so the middleware
opens one around the whole request.

This is not incidental. A plain `SET` would persist on the connection, and with
connection pooling **the next request — possibly another tenant's — would
inherit it**. That is the single worst bug available in this design, so it is
defended twice: the context manager restores the previous value on exit, and
the database discards it when the transaction ends regardless. Both are tested,
the second with a real committed transaction rather than a test-wrapped one.

`tenant_context()` raises if called outside a transaction, because there
`is_local` silently does nothing and every query would come back empty for
reasons that are very hard to find.

### Tables that belong to a business indirectly

A table can hold one business's rows without carrying a `tenant_id`, if it
points at something that does. Milestone 1's coverage test only looked for a
direct `tenant` column, so it walked straight past five such tables — all
created by Django or a third-party app rather than declared in this codebase:

| Table | Belongs to a business via | Consequence if unprotected |
|---|---|---|
| `token_blacklist_outstandingtoken` | `user_id` | **Stores the encoded refresh token itself in a text column.** A query made while one business was bound could read another's refresh tokens verbatim. |
| `token_blacklist_blacklistedtoken` | the row above | Inherits that exposure. |
| `accounts_user_groups` | `user_id` | Group membership across businesses. Empty in practice — roles are a field, not Django groups. |
| `accounts_user_user_permissions` | `user_id` | As above. |
| `django_admin_log` | `user_id` | Console activity. Its rows reference platform administrators, who have no business. |

Four were harmless in practice. The first was not: it is a credential
disclosure, not metadata.

These are protected with `enable_rls_via()`, which defines visibility by the
parent's visibility rather than by copying the tenant rule:

```sql
EXISTS (SELECT 1 FROM accounts_user p WHERE p.id = token_blacklist_outstandingtoken.user_id)
OR COALESCE(current_setting('app.bypass_rls', true), '') = 'on'
```

The subquery is evaluated as the querying role, so the parent's own policy
applies inside it. A row whose parent is invisible is therefore invisible too,
and the rule stays correct automatically if the tenant predicate ever changes —
there is no second copy to keep in step.

The coverage test now resolves ownership **transitively** over foreign keys,
across `apps.get_models(include_auto_created=True)` so the join tables Django
creates for itself are included. A table reaching a business through any depth
of relation must carry a policy or the build fails.

### How the platform reads across businesses

There are two distinct mechanisms, and it is worth being precise about which
applies where, because the answer is not the same for every table.

**The registry is simply not protected.** `tenants_tenant` carries no policy at
all. It is what isolation is *defined against*: sign-in resolves a business by
slug before any business is bound, so a policy here would make signing in
impossible. Access is an application-layer concern instead — only the platform
surfaces list it, and a business reads exactly one row, its own.

**Everything else is protected, and the console sets a flag.** Every
business-owned table has a policy permitting a row when the bound tenant matches
**or** when the session flag `app.bypass_rls` is on.
`TenantBindingMiddleware` sets that flag for exactly two URL prefixes — the
console and `/api/v1/platform/` — and every view behind them independently
requires `IsPlatformAdmin`. Nothing else in the system can turn it on, and a
test walks the URL conf to prove no route there is left open.

So a cross-tenant read is never an absence of protection. It is a protected
table plus an explicitly set flag, on a route that requires the platform
operator, inside one transaction.

Both directions are asserted together in
`apps/core/tests/test_platform_read_boundary.py`. Proving only that the operator
can read everything would still pass with isolation switched off entirely;
proving only that a business sees its own rows would still pass with the console
broken. The pair is what pins the boundary down.

### The escape hatch: migrations and backfills

A data migration, a management command or a shell session has no request and
therefore no bound business — so it sees **nothing at all**. That is the safe
default, but it is a confusing one to meet, because a migration that updates
zero rows reports success:

```python
# Silently touches nothing. Not an error.
Item.all_objects.all().update(cost_cents=0)
```

Two supported shapes, depending on the work:

```python
# Genuinely cross-business: one pass over everything.
from django.db import transaction
from apps.core.tenancy import bypass_rls

with transaction.atomic(), bypass_rls():
    Item.all_objects.all().update(...)

# Per-business work: bind each in turn. Preferable where it fits, because
# each iteration is confined and a bug cannot spill across businesses.
from apps.core.tenancy import tenant_context

for tenant_id in Tenant.objects.values_list("id", flat=True):
    with transaction.atomic(), tenant_context(tenant_id):
        ...
```

The transaction is required, not stylistic: the underlying setting is
transaction-scoped, and `tenant_context()` raises rather than let a binding be
made that silently does nothing. Inside a `RunPython`, Django already provides
the transaction, so `bypass_rls()` alone is enough.

Both shapes, and the zero-rows failure mode, are covered by tests in the
boundary file above.

### The bypass, and keeping it small

`bypass_rls()` lifts isolation. It is used by exactly two things: the platform
surfaces, and the bootstrap command that creates the first platform
administrator before any tenant exists. It is deliberately named plainly,
kept to one small module, and unreachable from any tenant-facing route.

---

## The generic catalog

There is one `Item` model covering both a tin of beans and a haircut.

```
Item
├── item_type      PRODUCT | SERVICE
├── name, sku, category, price_cents, cost_cents, tax_rate
├── track_stock    off for services and made-to-order food
└── duration_minutes   set for services, null for products
```

A service is an item that is not stock-tracked and has a duration. A product is
an item that is stock-tracked and has barcodes. Everything downstream — the
cart, the sale line, tax, discounts, receipts, reports — operates on items
without caring which kind it holds.

That is what stops the restaurant build becoming a fork of the retail build. If
there were `Product` and `Service` as separate models, every one of those
downstream components would need two code paths, and the fourth business type
would be a rewrite rather than a configuration.

**Barcodes are a separate table.** The same product genuinely arrives with
different codes — a supplier changes packaging, a multipack carries its own
code, a shop prints its own labels for loose goods. One barcode field per item
forces a shop to choose which code "counts", and the one they did not choose
then fails to scan at the counter.

**Item identity is business-wide, quantity is per store.** That split
(`Item` versus `inventory.StockItem`) is the multi-branch seam. Adding a second
branch is an insert; hanging quantity off the item itself would make it a
redesign with live data to migrate.

---

## Modules, not forks

A business type is a *template*: a set of defaults applied once at setup, and
nothing more. It selects which modules start enabled and how tax is presented.
Everything it sets stays editable, and the type can be changed later without
consequence.

```
apps/tenants/templates_registry.py    plain data, no code paths

RETAIL      → stock, mpesa, compliance
RESTAURANT  → stock, mpesa, compliance, restaurant
SALON       → mpesa, compliance, appointments
PHARMACY    → stock, mpesa, compliance, pharmacy_batches
```

Module-specific data lives in tables owned by that module, pointing back at the
shared models:

```
inventory.StockItem        item × store → quantity          (milestone 2)
restaurant.ModifierGroup   item → "no onions"               (milestone 6)
appointments.Appointment   item + staff + time              (milestone 7)
pharmacy.StockBatch        stock_item → batch, expiry       (milestone 8)
```

A retail shop touches none of those tables. Enabling the pharmacy module adds
`StockBatch` rows; it does not alter `Item`. `TenantModule` is a table rather
than a list field on `Tenant` for two reasons: it carries per-module
configuration, and it is queryable across tenants — which is exactly the
question billing asks ("who is using the restaurant module?").

Every module gets a row for every tenant, including the disabled ones, so that
"switched off" and "never considered" are not two different states meaning the
same thing.

---

## Authentication

JWT via `djangorestframework-simplejwt`. Stateless, which matters because a
till that has been offline for an hour should not find its session expired the
moment it reconnects.

**The token carries the tenant.** The `tenant_id` claim is what the middleware
binds isolation from, read without a database round trip — which is what breaks
the circularity described above. The claim decides what is *visible*; it never
decides what is *allowed*. Authorisation reads the user record from the
database, so a token cannot grant a role its owner does not hold.

**Usernames are unique per business, not globally.** Two shops can each employ
a cashier called `mary`. Global uniqueness would mean the second shop to sign up
finds its staff names already taken by strangers — a bad experience caused
entirely by an implementation detail. The cost is that sign-in needs a business
identifier, which the till stores once at setup and sends automatically
thereafter.

**Two sign-in routes, and they do not overlap.** Tenant staff sign in at
`/api/v1/auth/login/` with a business slug. The platform operator signs in at
`/api/v1/platform/auth/login/`, which reads only accounts with no tenant.
Neither can be used to reach the other.

**PIN sign-in is device-bound.** A cashier taking over the till mid-shift
enters a four-digit PIN, not a password — typing a password on a tablet between
customers is real friction, and friction at the counter is how you end up with
one shared login and no cashier accountability at all.

A four-digit secret is only acceptable because it is half a credential: it is
accepted only alongside a registered device token, so possession of the till is
the other half. PIN sign-in cannot be used from an arbitrary client. Device
tokens are 32 bytes of system randomness, stored as a SHA-256 hash, and shown
in plaintext exactly once at registration.

**PIN sign-in is bounded in total, not just in rate.** A rate limit caps how
*fast* attempts arrive; it does not cap how *many*. Against a four-digit space,
a patient attacker holding a stolen till simply works within the limit. So there
is a lockout as well: five consecutive failures and that till is refused for
fifteen minutes, however slowly the attempts came in, and the correct PIN does
not help while it lasts.

Two counters, because there are two attacks. A **device** counter stops many
PINs being tried against one till — the stolen-tablet case. A **user** counter
stops one cashier's PIN being tried across several tills, which a per-device
limit alone would miss entirely. They are independent: locking one till leaves
the shop trading on another, while locking a cashier follows them everywhere.

An unregistered device token is deliberately **not** counted. Counting it would
let anyone lock out a till they cannot otherwise touch by sending rubbish
tokens, turning the protection into a way to stop a shop trading.

Counters live in Redis rather than the database — they are high-frequency,
worthless once expired, and must be shared across API workers, since a
per-process count would hand an attacker the full allowance once per worker.
Every failure is also written to the audit trail, which is the durable record: a
manager investigating a missing float needs to see that someone sat trying PINs
at eleven at night, and a counter that expires in fifteen minutes will not tell
them that. The entry is filed against the *username string*, with no user
attached, because at that point nobody has proved they are that person and
filing it against them would put someone else's guessing into an innocent
cashier's history.

**Roles are ordered**, so checks read as "this role or above":

| | Cashier | Manager | Owner |
|---|---|---|---|
| Sell, read catalogue and branches | ✅ | ✅ | ✅ |
| Register a till, create a branch | ❌ | ✅ | ✅ |
| Voids, refunds, stock adjustments *(milestone 3)* | ❌ | ✅ | ✅ |
| Add or deactivate staff | ❌ | ❌ | ✅ |
| Business settings, tax, branding | ❌ | ❌ | ✅ |

`is_platform_admin` is a separate flag rather than a fourth role, and a database
constraint forbids it on anyone belonging to a business. No amount of privilege
escalation inside a tenant can produce cross-tenant reach.

**Suspension.** A business's status is cached for 60 seconds, so a busy till is
not making an extra query per request, but suspending a customer takes effect
within a minute rather than whenever their tokens happen to expire. A suspended
business gets `402 Payment Required`, not `403`, so the till can show "contact
your provider" rather than a generic permission error.

---

## Money

**Every monetary amount is an integer number of cents.** Not a float, not a
Decimal column. A test walks every model and fails the build on any
`FloatField`.

Floats cannot represent `0.10` exactly, so a day of sales accumulates error
that shows up as a till that will not balance — and looks like theft rather
than arithmetic. Integers make the set of representable amounts the same as the
set of amounts a cash drawer can actually hold.

**Rounding is half-up, not banker's rounding.** Half-up is what a person does
by hand and what makes a receipt total match a customer's own arithmetic.
Being "more correct on average" is worth nothing next to a customer disputing a
receipt they added up themselves.

**Tax rates are basis points.** 16% is `1600`. An integer rate keeps the whole
calculation in integer arithmetic, and no rate can arrive as a float that is
almost but not exactly 16%.

**Inclusive tax is derived by subtraction.** Given a VAT-inclusive price:

```
tax = round_half_up(gross × rate ÷ (10000 + rate))
net = gross − tax
```

The net is subtracted rather than calculated independently, which guarantees
`net + tax == gross` for every possible amount and rate. Calculating both and
rounding each separately is exactly how receipts end up a cent out; the test
suite asserts the property across two thousand amounts and four rates.

**`is_inclusive` lives on the tax rate, not on the tenant.** A single business
may sell VAT-inclusive over the counter and quote VAT-exclusive to a trade
customer. `Tenant.vat_mode` only supplies the default when a rate is created.

**Cash rounds to the shilling.** There is no coin below KES 1 in practical
circulation. Card and mobile money settle to the cent; cash cannot. The
difference is recorded on the sale so a till reconciles exactly rather than
drifting a few shillings a day.

---

## Offline sync

*Designed now, built in milestone 3. Documented here because it constrains
decisions in every earlier milestone.*

The core rule: **a completed sale is a fact, not a request. The server never
rejects one.** Money has already changed hands; refusing the record only loses
data.

### What the device holds

Two SQLite tables via drift: a mirror of the catalogue, and an outbox of sales
waiting to sync.

### Catalogue pull

`GET /api/v1/sync/catalog/?since=<cursor>` returns deltas — items, barcodes,
prices, tax rates — using an `updated_at` cursor plus tombstones for deletes.

### Idempotency

Every sale carries a client-generated `client_uuid`, a `device_id` and a
monotonic device sequence, all created on the device before any network call.
The server has `UNIQUE(tenant, client_uuid)`; replaying a batch returns the
original sale with `200`, never a duplicate with `201`.

This matters more than clean-disconnect handling. The real failure mode on
Kenyan mobile data is not a connection that drops — it is a request that hangs
for ninety seconds and *succeeded invisibly*. Without an idempotency key, the
retry is a duplicate sale. This is also why primary keys are UUIDs: a
disconnected till must be able to create a sale, reference it from its lines and
payments, and sync the whole graph later without renumbering anything.

### No merge conflicts, by construction

Sales are append-only and immutable. Corrections are *new* documents — a void
or a refund referencing the original. There is no update path, therefore there
is no merge to get wrong. This is the single decision that makes offline sync
tractable rather than a distributed-systems problem.

### Price snapshotting

Each sale line denormalizes the item name, unit price, discount, tax rate
**and** `is_inclusive` as they were at the moment of sale. A price change or a
VAT-mode change during an outage cannot retroactively rewrite yesterday's
receipts.

### Stock conflicts

Device quantity is advisory; the server is authoritative. If an offline sale
drives stock negative, the server **accepts the sale** and raises a
`StockDiscrepancy` for the manager to reconcile.

This is a deliberate trade. Refusing a sale whose cash is already in the drawer
would mean the shop's books do not contain money the shop physically has, which
is far worse than a stock count that needs correcting.

### Suspension and offline devices

A device that was offline while its business was suspended can still sync sales
it already completed — that money was taken. It is blocked from starting new
ones. Sync accepts; checkout refuses.

### Clock skew

Both `device_created_at` and `server_received_at` are stored. All reporting uses
server time, because a till with a wrong clock must not be able to move revenue
between days.

---

## M-Pesa

*Milestone 3.* STK Push via Safaricom's Daraja API: the cashier enters the
amount, the customer gets a prompt on their phone, and the callback confirms.

**M-Pesa is online-only, by definition.** STK Push requires connectivity, so
when the device is offline it disables the M-Pesa tender and offers cash only,
rather than queueing a payment that may never land. This is why offline sales
are cash sales.

Payment records are separate from the sale, so split payments (part cash, part
M-Pesa) and partial refunds are ordinary rows rather than special cases.

---

## Auditing

Audit entries are written by calling `record_audit()` explicitly, not by
hooking `post_save` signals.

Signals would catch more writes with less code, but they cannot see the two
things that make an audit trail worth keeping: **who** was acting and **why**.
A signal fires with a model instance and no request, so every entry would read
"something changed" with no actor and no reason — which is precisely the
information a shop owner needs when the stock does not match the shelf.

The trail records the actor as text as well as by foreign key, so it survives
the user being renamed or removed. Credential-shaped fields are redacted before
storage, because managers can read the audit trail and it must not become a
place where password hashes accumulate.

Every destructive action — deactivating a user, revoking a till, and later
voids, refunds and stock adjustments — is both role-gated and audited. The
pairing is deliberate: if a boundary is worth enforcing, the crossing is worth
recording.

---

## Technology choices

| Choice | Why |
|---|---|
| **Django + DRF** | The tenant isolation strategy depends on middleware, a custom user model and migration-level SQL. Django does all three well. DRF's serializers give input validation and correct status codes by default rather than as an add-on. |
| **PostgreSQL** | Row-Level Security. The entire isolation strategy is a Postgres feature; no other database in reach offers an equivalent. |
| **Docker Compose, single route** | The isolation guarantees depend on connecting as a specific non-superuser role. A parallel "without Docker" path would be a second setup to keep correct, and the one that silently disables every guarantee if it drifts. |
| **`drf-spectacular`** | OpenAPI generated from the serializers and views themselves, so documentation cannot drift from the implementation. |
| **Flutter** | One codebase for the Android phones and tablets small retailers actually own, with genuinely native-feeling touch targets. |
| **drift (SQLite)** | Typed SQL on the device for the outbox and catalogue mirror. Offline queueing is the trickiest part of the client and benefits from compile-time query checking. |
| **Riverpod** | Testable state management without a widget tree, so the sync and cart logic can be unit-tested away from the UI. |
| **JWT over sessions** | Stateless, so a network drop does not invalidate anything server-side, and a refresh token lets a till run for a fortnight. |
| **Django admin as the platform console** | Everything the operator needs is CRUD over a handful of models. The REST endpoints exist alongside it, so a purpose-built dashboard later is additive rather than a rewrite. |
| **Redis** | Two pieces of state that must be shared across worker processes and are worthless once expired: per-business suspension status, and PIN lockout counters. A per-process cache would let each worker keep its own view of both, which would quietly weaken the lockout. |

### Failing loudly on unchanged configuration

Some settings are harmless in development, must be changed before deployment,
and give no visible sign when they have not been. Those are guarded by Django's
own check framework, registered at `Error` level so `runserver`, `migrate` and
every management command refuse to run — which usefully means a bad
configuration fails the deployment's migrate step, not just the web process.

| Check | Guards |
|---|---|
| `pos.E001` | `PLATFORM_ADMIN_URL` still the placeholder published in `.env.example` |
| `pos.E002` | It set to `admin/`, or empty, which would mount the console at the site root |
| `pos.E003` | `SECRET_KEY` still the development value — it signs every access token on the platform |
| `pos.W001` | A per-process cache in a deployment, which weakens PIN lockout |

All are inert when `DEBUG` is on: the placeholders are the point in development,
and failing locally would be friction with no security benefit. The placeholder
stays in `.env.example` as documentation of the expected shape — the check is
what makes forgetting to change it loud rather than silent.

---

## What is deliberately not here

- **Subscription billing.** Usage counts are reported for invoicing out of
  band. Putting payment processing inside the product would tie a shop's
  ability to trade to an integration that can fail independently of it.
- **Tenant deletion.** Businesses are suspended, never deleted. Their sales are
  a legal record their owner may need years later, and a cancelled customer who
  returns should not start again.
- **User deletion.** Deactivation only, so the audit trail and sales history
  keep the name attached to them.
- **Global barcode uniqueness.** Two unrelated shops printing their own labels
  will collide, and neither is wrong.
- **eTIMS integration.** Milestone 4 builds a compliance adapter interface with
  a manual/export implementation. A real OSCU/VSCU integration plugs into that
  interface later without touching the sales schema.
