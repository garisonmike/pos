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
9. [Printing](#printing)
10. [M-Pesa](#m-pesa)
11. [Shifts and the cash drawer](#shifts-and-the-cash-drawer)
12. [Compliance](#compliance)
13. [Reporting](#reporting)
14. [Auditing](#auditing)
15. [Technology choices](#technology-choices)
16. [What is deliberately not here](#what-is-deliberately-not-here)

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

**Prices are business-wide, with the seam left open.** One price per item across
branches, which is how most Kenyan chains operate. If a depot ever needs to
price differently from a kiosk, a `StorePriceOverride` table resolves as
`override ?? item.price` without touching `Item` or any sale code.

**Two flags that a retail-only build would have forgotten.** `is_available` is
separate from `is_active`: "delisted" and "off today" are different states, and
a salon needs "fully booked" exactly as much as a duka needs "out of season".
`is_price_variable` marks items whose price the cashier enters — a service
quoted on the day, or damaged retail stock. Both live on `Item` rather than in a
product-only table, because putting them there is what makes services
second-class.

### Stock

Two models, and the split is the point.

```
StockItem      item × store → quantity, reorder_level
StockMovement  append-only: delta, balance_after, reason, note, user, ref
```

`StockItem.quantity` is a **cache** of the ledger, rebuildable from it at any
time. Keeping both looks redundant until a count disagrees with the shelf: the
cached number answers *how many*, and only the ledger answers *why* — which is
the question a shop owner actually asks.

Quantities are `Decimal`, not integers, because loose goods are real: sugar and
flour come out of an open sack by weight. Money stays integer cents.

**Nothing moves without a reason and an author.** Adjustments, wastage and count
corrections all require a note, enforced in the service layer as well as the
serializer so it holds however the code is called. Sales and refunds are refused
as manual adjustments outright — they carry a sale reference instead, and
allowing them here would let someone move stock as if sold with no sale behind
it. That pairing is deliberate: adjusting stock is how a theft gets covered up,
so the role boundary and the record of crossing it belong together.

**Stock may go negative.** Refusing would mean refusing to record something that
has already happened, which puts the books further from the truth than a
negative number does. It is surfaced with a warning for a manager to reconcile.

`apply_movement()` takes a row lock. Two cashiers selling the last unit at the
same moment would otherwise both read the same quantity and write back totals
that ignore each other — the classic lost update, which shows up as stock that
will not reconcile rather than as an error anyone notices.

### Bulk import

A client's product list is reliably the slowest part of onboarding, not the
software. So import is built for the shape that work really takes: a spreadsheet
exported from wherever the prices currently live, inconsistent in a dozen small
ways.

**Validate, then commit.** Validate writes nothing and reports every row.
Commit imports what passed and reports the rest — valid rows land even when
others fail, because refusing four hundred good rows over three bad ones means
an afternoon of editing before anything works at all.

Two details stop the phases disagreeing:

- The token is tied to a **hash of the file**, so commit cannot be pointed at a
  different file than the one whose report was reviewed. It expires in an hour.
- Commit **re-resolves** every category and tax rate rather than trusting what
  validate found. Someone may rename one while the report is being read, and
  that row must then fail like any other bad reference while the rest still
  import.

Unknown **categories are created**; unknown **tax rates are not**. A category is
a free-form label and pre-creating thirty is friction with nothing to show for
it. A typo creating `VAT 16 %` at the wrong value would silently mis-tax every
sale filed against it from then on.

One consequence worth being explicit about: a commit is **not** a clean no-op
when rows fail. Good rows import and named categories are created, which is the
direct cost of per-row handling over all-or-nothing.

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

The core rule: **a completed sale is a fact, not a request. The server never
rejects one.** Money has already changed hands; refusing the record only loses
data.

### What the device holds

Five drift/SQLite tables, in `mobile/lib/data/outbox/database.dart`:

| Table | Why it exists |
|---|---|
| `queued_sales` | Sales rung up with no connection, keyed on `client_uuid`. Rows are deleted only on a verdict from the server, never on a hopeful assumption that the upload worked. |
| `pin_attempts` | The offline lockout counter, and the refusals waiting to be sent home. See below. |
| `sync_cursor` | The `server_time` the last catalogue download returned, stored as the exact string the server sent. |
| `catalog_cache` | The price list, flattened for pricing a cart with nothing to ask. |
| `staff_cache` | Staff, with `pin_version` and — for managers only — `pin_hash`. |

### Catalogue pull

`GET /api/v1/sync/catalog/?since=<cursor>` returns everything with an
`updated_at` after the cursor. Withdrawn items are sent **with `is_active`
cleared, not omitted**: a till that never hears about a withdrawal carries on
selling the thing, and a queued sale that already includes it still needs a name
to print.

The cursor is always the `server_time` the previous response carried, never the
till's own clock. A till running fast would otherwise ask for a window that
skips changes it never saw.

`server_time` is read *before* the queries run, not after. Taken afterwards, a
row written while the queries were running would fall before the returned
timestamp and never be sent again — a price change lost for good.

### Sending the backlog up

`POST /api/v1/sync/sales/` takes a whole batch and answers with a **verdict per
sale** — `accepted`, `duplicate` or `rejected` — rather than one status for the
request. One bad sale in a batch of forty must not strand the other
thirty-nine, and the till has to know exactly which rows it may delete from its
outbox. The batch is `200` whenever it was *understood*, even if every sale in
it was refused.

A batch also carries `refused_authorizations`: the discount approvals the till
turned down while it had no connection (see the lockout section below).

### The device must belong to the business

Every batch names a `device_id`, and it is resolved with a **tenant-scoped
lookup** rather than fetched and then compared:

```python
Device.objects.filter(pk=device_id, is_active=True).first()
```

Another business's device id is simply not found. There is no cross-tenant
comparison to get wrong and no branch that could accidentally accept one, which
is exactly why it is written as a lookup — `device.tenant_id == tenant.id` needs
the row first, and fetching the row is the mistake. A batch that names an
unknown till is refused with `unknown_device` and recorded as a
`SaleDiscrepancy` against the business that sent it.

### Idempotency

Every sale carries a client-generated `client_uuid`, a `device_id` and a
monotonic device sequence, all created on the device before any network call.
The server has `UNIQUE(tenant, client_uuid)`; replaying a batch returns the
original sale with `200`, never a duplicate with `201`.

**Idempotency is the database's job, not a lookup's.** The obvious shape — look
for an existing sale, create one if absent — is a race with a window between the
two statements, and two threads uploading the same batch will both find nothing
and both insert. So `replay_sale` **inserts first** and treats `IntegrityError`
on `unique_sale_client_uuid_per_tenant` as the duplicate signal. The constraint
is the arbiter because the constraint is the only thing that is actually atomic.
`apps/sync/tests/test_concurrent_replay.py` proves it with real threads against
`TransactionTestCase`; the usual per-test transaction would hide the race
entirely, because inside one transaction the threads cannot see each other at
all.

The key is scoped `(tenant, client_uuid)` rather than globally unique, so that a
collision between two businesses — by chance or on purpose — cannot make one
shop's sale silently vanish as somebody else's duplicate.

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

### Offline discount authorisation

A discount needs a manager's approval. Online, that approval is a username plus
their PIN verified server-side, where the PIN lockout applies. Offline, the
device verifies against its cached copy, and three separate controls stand in
for the server that is not there.

**A lockout in the outbox database.** The server counts failed attempts in
Redis, and Redis is precisely what cannot be reached. So `pin_attempts` holds
the count on the device, with the same thresholds as the server
(`PIN_LOCKOUT_MAX_ATTEMPTS = 5`, `PIN_LOCKOUT_SECONDS = 900`) — two different
numbers would mean a manager who mistypes twice is locked out at the counter but
not in the back office, and the shop would learn to distrust whichever one
refused them.

```
pin_attempts(
  scope_key TEXT PRIMARY KEY,   -- the username typed, not a user id
  failures INTEGER,
  locked_until INTEGER,         -- epoch ms, null when open
  last_failure_at DATETIME,
  pending_telemetry_json TEXT   -- refusals waiting to sync
)
```

It is keyed on the **username typed**, not a resolved user id, so a name that
matches nobody is rate-limited on the same footing as one that does — otherwise
the counter itself answers "does this name exist?" and the staff list can be
enumerated by watching which names lock out. It is not cleared by reconnecting:
a lockout liftable by toggling aeroplane mode would not be a lockout. And an
attempt made *while* locked out is recorded but does not extend the window, so a
bystander tapping at the screen cannot keep a manager shut out indefinitely.

**Every refusal syncs home.** `pending_telemetry_json` is sent with the next
batch and written as `DISCOUNT_REFUSED` audit entries, filed against the
attempted username as a bare string with no user foreign key — exactly as an
online refusal and a failed sign-in are, because nobody proved they were that
person. Without this, somebody spending an evening on a manager's four digits at
a disconnected till would leave a record only on the tablet in their hand.

**A PIN version fingerprint.** Every `set_pin` and `clear_pin` bumps
`accounts.User.pin_version`. The till caches that number beside the hash it
verifies against, and sends it back with any offline approval:

| Field | Where | What it holds |
|---|---|---|
| `accounts.User.pin_version` | server | Bumped on every `set_pin` / `clear_pin`. The source of truth. |
| `staff_cache.pin_version` | device | The version of the cached copy the device checks against. |
| `discount_authorization.pin_version` | wire | What the device reports it checked against. |
| `sales.Sale.discount_authorized_pin_version` | server | Recorded on the sale, for OFFLINE approvals only. |
| `sales.Sale.discount_authorization_is_stale` | server | Set at sync when the two do not match. |

**Be precise about what this establishes.** It reliably catches a device
approving against a PIN that has since been *changed or revoked* — a till that
was offline across either event reports a number that no longer matches, and the
sale is flagged with a `STALE_AUTH` discrepancy. It does **not** prove the
device performed the check: anyone who can edit the till's local database can
also read the version that database holds, so a fabricated payload built on a
current cache carries a matching version. Detecting that would need the device
to hold a secret the fabricator cannot read, which a rooted Android tablet does
not offer. The claim is the narrow one — staleness and revocation, not forgery.

The server also re-checks the named authoriser's **role** at sync, so a till
claiming a cashier approved a discount is caught regardless of versions.

A stale or invalid authorisation never rejects the sale. By the time sync runs
the customer has walked out with the goods; refusing the record would delete the
evidence rather than the problem.

### A till that undercharged

The likeliest offline failure there is: a price goes up while a till is
disconnected, the till collects yesterday's lower price, and at sync the server
prices the cart higher than the cash that came in.

`take_cash` refuses that with `insufficient_tender`, and it is right to for a
**live** sale — the cashier is holding too few notes and the customer is still
standing there, so the correct answer is to ask for the rest. A sale arriving
through **sync** is finished. The customer paid what they were asked and left
with the goods.

So sync accepts it. The difference is written to `Sale.offline_shortfall_cents`,
the ledger closes against it, and the sale settles `PAID` for what was actually
collected — stock moving as normal, because the goods really did leave. An
`OFFLINE_SHORTFALL` discrepancy records the amount and its reason.

The shortfall is its own quantity rather than a reduced total, so that what the
sale *should* have come to and what the shop *settled for* stay separately
visible. Folded into the total it would be indistinguishable from a cheaper sale
a week later, and a till running a stale price list for a month would never be
spotted. Nothing in code decides what the shop does about the gap — that is a
person's call after the fact, like every other discrepancy here.

### What `LedgerPosition` actually measures

Settlement is decided against `collectable_cents`, never against `total_cents`:

```
collectable = total_cents + rounding_adjustment_cents - written_off_cents
```

A sale is finished when the customer has paid what they were **asked**, which is
not the same number as what the goods listed at. Cash rounds to the shilling,
and a shortfall may have been written off; both change the figure the customer
was actually charged.

This was a real bug, not a theoretical tidy-up. `ledger_position` read
`total_cents` raw while `take_cash` charged the rounded figure, so the two
disagreed by up to fifty cents and **any** cash sale whose total was not a whole
shilling took the customer's money and never settled — no receipt number, no
stock movement. With VAT-inclusive pricing that is most sales.
`initiate_stk` had been adding the adjustment back by hand at its own call site,
which is what a missing concept looks like from the inside.

### Open: a stolen till is a stolen manager PIN

`GET /sync/catalog/` sends `pin_hash` down so an offline check has something to
verify against. It is sent **only for users who can actually authorise** — a
cashier's hash is never downloaded, because no offline check would consult it.

The cost is real and worth stating plainly: a PIN is four to six digits, so a
hash of one is brute-forceable by anyone holding the tablet, and that same PIN
works online. Approving anything offline against a short secret has this shape
inherently — a local check needs a local verifier.

The stronger fix, deferred: a **separate offline approval code**, versioned the
same way, revocable on its own and useless for signing in. Then a stolen till
gives up only the thing that can be rotated without touching anyone's sign-in.
Not built yet; recorded here so it is not discovered later as a surprise.

### What the till claimed it came to

A batch may carry the total the till believed a sale came to. It is **never
trusted** — the server prices every cart again from its own catalogue. A
disagreement is recorded as a `TOTALS_MISMATCH` discrepancy and the sale still
lands, because the goods have already left the shop. What a disagreement usually
means is a till carrying a price list from before the last change, which is
worth a person's attention rather than a rejection nobody sees.

### Pricing the cart twice, on purpose

`mobile/lib/data/cart/pricing.dart` is a **second implementation** of
`backend/apps/sales/pricing.py`. Two implementations of the same arithmetic is
normally a mistake; here the alternative is telling a cashier there is no total
until the network comes back, with a customer waiting.

Two things keep them honest:

1. **The server always re-prices on arrival** and never trusts the device's
   figure. A disagreement becomes a `TOTALS_MISMATCH` discrepancy, so drift
   surfaces as something a person reads rather than as money going missing.
2. **A parity fixture generated by the server.** `backend/gen_pricing_fixture.py`
   prices 120 awkward carts — fractional quantities, mixed inclusive and
   exclusive tax on one sale, line and cart discounts together — and
   `mobile/test/pricing_parity_test.dart` asserts the Dart reproduces every
   figure to the cent. If it fails after a change to either file, the question
   is which one is right, not which test to update.

Quantities are integers of thousandths on the device, for the same reason money
is integer cents: a double would occasionally price 0.333 kg of sugar a cent
away from the server.

### One identifier, generated before the attempt

The till generates `client_uuid` **before** it tries the online checkout, and
reuses the same one if the sale has to be queued.

This is the whole defence against the normal failure on Kenyan mobile data,
which is not a connection that drops but a request that hangs for ninety seconds
and *succeeded invisibly*. A fresh identifier at queue time would make the sync
create a second sale for money taken once. With the same one, the server
recognises the replay and answers `duplicate`, which costs nothing.

The same reasoning decides what gets queued at all:

| Failure | What happens | Why |
|---|---|---|
| Connectivity, or any 5xx | Queued | The server may or may not have written it, and the cash is in the drawer either way |
| A refusal the server means — unknown item, unauthorised discount | **Not** queued | Retrying unchanged reproduces the same refusal at sync, with the cashier no longer at the counter to fix it |

### Clock skew

Both `device_created_at` and `server_received_at` are stored. All reporting uses
server time, because a till with a wrong clock must not be able to move revenue
between days.

---

## Printing

ESC/POS, the command language every cheap Bluetooth till printer speaks. Byte
building is kept **pure and separate from sending**, in
`mobile/lib/data/printing/escpos.dart`, because the thing that goes wrong is not
the escape codes — it is a line one character too wide, which wraps every row on
a real printer and is invisible until somebody prints one in a shop. The width
is asserted in tests against a decoded byte stream.

Two details that only show up on real hardware:

* **Double-width mode halves the character count.** A 32-character shop name at
  double width overflows exactly as badly as a 64-character one at normal size.
* **The code page is fixed.** Text is encoded as Latin-1 and anything outside it
  is *replaced*, not dropped — dropping a character silently changes what the
  receipt says.

A printer that is out of paper, unpaired or switched off never fails a sale. The
money is already in the drawer and the record is already made; the receipt is
the one part of a sale that can be retried at leisure.

## The till application

Flutter, Android first. Currently signs in and browses; selling is milestone 3.

**Three states, one rule.** The app is in exactly one of: *unclaimed* (no
business chosen), *claimed* (business known, device not registered, so a
password is the only way in), *registered* (PIN sign-in available), or *signed
in*. The root widget switches on that and nothing else, so "why am I looking at
sign-in" has one readable answer rather than being decided by screens pushing
each other around.

**The device token is a credential, not configuration.** It looks like a
setting — written once, never changed — but combined with four digits it signs a
cashier in. It goes in the platform keystore with the access and refresh tokens,
never in shared preferences. Signing out clears the tokens and *keeps* the
device token, which is exactly what lets the next cashier take over with a PIN.

**Refresh lives in an interceptor.** Access tokens are deliberately short-lived
and a till sits idle between customers, so any screen can be the first to meet an
expired one. Handling it per call site would fail on whichever screen the author
forgot. A single in-flight guard stops a burst of parallel requests each
triggering a refresh — with rotation on the server, the second and third would
present a token the first had already replaced and sign the cashier out
mid-shift.

**Offline is not the same as signed out.** If the session check fails because
the network is down, the app keeps the stored session rather than bouncing a
cashier to sign-in over a dropped bar of signal.

**The UI constraints are functional, not aesthetic.** A duka counter is often
near an open front, so contrast is pushed well past the usual minimum — no grey
text carries meaning. Tap targets are 56pt minimum and PIN keys are 72, because
the person tapping is often holding a bag of shopping. Primary actions sit at
the bottom where a thumb lands. The PIN pad submits on the fourth digit rather
than making someone find a button they would have to look at.

**"Not tracked" is shown differently from "none left".** A haircut with `0`
beside it reads as sold out, and a cashier would hesitate over something they
should simply sell. Untracked items show no stock line at all.

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

## Taking money by M-Pesa

STK Push via Daraja: the cashier rings up, the customer gets a prompt on their
phone, and a callback confirms. Each business brings its **own** credentials —
Safaricom issues production ones to a registered business, not to whoever runs
the software — encrypted at rest with a key that is deliberately not
`SECRET_KEY`, since rotating Django's secret must not destroy every tenant's
payment configuration.

### Four idempotency keys

Each catches a different real failure, and each is a database constraint rather
than an intention:

| Key | Stops |
|---|---|
| `PaymentIntent.client_uuid` | the till retrying its own request after a timeout |
| `PaymentIntent.checkout_request_id` | two intents claiming one Daraja push |
| `MpesaCallback.checkout_request_id` | **Safaricom retrying a callback**, which it does routinely |
| `Payment.mpesa_receipt_number` | one real movement of money credited twice by *any* path |

A duplicate returns the same acknowledgement as the first delivery. Anything
else would make Safaricom retry a callback we have deliberately refused.

### One guarded settlement path

`settle_intent()` is the only way M-Pesa money reaches a sale. The callback uses
it; so does the reconciliation job. Two implementations of "is this sale still
creditable" would eventually disagree, and the disagreement would show up as a
customer charged twice.

The guard reads **ground truth**, exactly as `void_sale` does — and the split is
worth being precise about:

- *State facts* — void, and whether a prompt is outstanding — have no ledger row
  to derive them from, so they come from the state column.
- *Money facts* — has this been paid, has it been refunded — are summed from the
  ledger every time.

So a sale whose column says `AWAITING_PAYMENT` but whose payments already cover
it is **refused**, because the money is what decides. There is a test that forces
the column to lie and confirms the callback is held rather than applied.

Anything held becomes a `SUSPECT` callback and raises a discrepancy. Not
credited, not dropped: money the shop may be holding and has not applied.

### How a callback finds its business

A callback carries no tenant and no credential, so the token in its URL is the
only thing that can resolve it — 32 bytes of randomness, derived from **nothing**,
because anything derived is a value someone can attempt to construct.

That lookup is the third and last place `bypass_rls()` is reached, and it is one
statement: a single `SELECT ... WHERE callback_token = %s LIMIT 1`, no joins, no
writes, nothing else in the block. The moment an intent is found its tenant is
bound and everything after is ordinary tenant-scoped code. A test asserts this
prefix contains exactly one route.

### The IP allowlist, and the proxy assumption

**The deployment assumption: exactly one trusted proxy in front of Django** — a
single nginx, Caddy or load balancer terminating TLS, matching the
single-Compose-route shape this already deploys in.

That assumption matters because `X-Forwarded-For` is built left to right and
each proxy *appends* what it saw:

```
X-Forwarded-For: <whatever the client sent>, <address our proxy saw>
                  ^ unverified, attacker-controlled  ^ the only entry we believe
```

So the **last** entry is read, not the first. Reading the first — the common
default, and what this code originally did — is exactly backwards for a security
decision: it is the entry most under an attacker's control, and anyone could
claim to be Safaricom by typing an address into a header.

How many hops to count back is configuration, and **production refuses to start
without it** (`pos.E004`), because neither guess is safe: too few reads a
caller-supplied entry and credits forgeries, too many reads nothing and refuses
every real callback.

The allowlist itself applies **only on production credentials**. Sandbox skips it
entirely, so a business integrating from behind an unpredictable address is
never blocked while no real money is moving. A production business with an
**empty** list fails closed — "not configured" must not look identical to
"configured correctly".

### Lost callbacks, and why reconciliation exists

A lost callback is at least as likely as a duplicated one, and no amount of
idempotency helps with a message that never came. `reconcile_mpesa` asks Daraja
what happened to any prompt that has gone quiet, run from cron every couple of
minutes.

It cannot double-credit against a callback landing at the same moment: both take
`select_for_update` on the intent first, so one waits for the other and the
loser finds it no longer pending.

One wrinkle worth knowing: Daraja's status query confirms *whether* a payment
succeeded but does not return the M-Pesa receipt code — only the callback carries
that. Crediting with a blank reference would disable the one constraint that
catches a double credit by any path, so a reconciled payment carries the
checkout request id under a visible `RECON-` prefix instead: unique per attempt,
and obviously not an M-Pesa code. A late callback then finds the intent settled,
is recorded rather than credited again, and its payload preserves the true
receipt code on the callback row.

### Overpayment

Two successful pushes genuinely charge a customer twice. Both payments are
recorded — refusing to record money the customer actually sent would put the
books further from the truth — and `is_overpaid` blocks completion until a
manager refunds the difference. To keep this rare rather than merely survivable,
a second push is refused while one is still live on that sale.

### Refunds

Recorded and settled outside the system. Automated M-Pesa refunds need the B2C
product, its own credentials and a funded float, which a small shop does not
have on day one — so the ledger stays correct and the shop settles it the way
they already do. An unsettled refund stays visibly unsettled.

---

## Shifts and the cash drawer

A shift is one cashier, one drawer, one till, for one stretch of time. Closing
one is the moment a person counts money and puts their name to a figure, which
makes it the most sensitive record here after the sales themselves.

Shifts are **optional**. A duka with one person and one drawer may never open
one, and it goes on selling exactly as before — `Payment.shift` is simply null.

### The count is blind

A cashier declares what is in the drawer *before* the system tells them what it
expected. This is enforced at the API, not merely in the interface: no endpoint
reports an expected total for a drawer that is still open, and
`expected_closing_cents` is computed inside `close_shift` only once the declared
figure is in hand. Showing it first turns the count into typing a number back,
and the control becomes theatre — which is worse than no control, because it
looks like one.

### Cash only

```
expected = opening_float + cash_sales - cash_refunds + paid_in - paid_out
```

An M-Pesa payment never touched the drawer. Folding mobile money in is how a
till reads twenty thousand short every day until nobody trusts the
reconciliation at all. The same reasoning excludes M-Pesa refunds, which are
settled outside the drawer.

`CashMovement` covers what no sale accounts for: `PAID_IN`, `PAID_OUT`, and
`DROP` for excess moved to the safe mid-shift. A reason is mandatory — "cash
out" with no reason is indistinguishable from theft, and the person who has to
tell them apart is reading it months later. Movements are append-only; a
mistake is corrected by an opposite movement, never an edit.

### A variance never blocks the close

The count is a fact to record, not something to argue with, and a shop that
cannot close its till stops trading. A non-zero variance writes a `VARIANCE`
discrepancy carrying the full breakdown — float, cash sales, refunds, paid in,
paid out — so a cashier who is nine hundred short can see which line it should
have come from rather than only that it is missing.

The optional denomination breakdown must sum to the declared total or the close
is refused. Picking a winner between two disagreeing figures would hide which
one the cashier got wrong.

### Closing figures are frozen

Once closed, a shift's expected total and variance never change. There is no
reopening; a correction is a new record.

A payment names its shift **explicitly** rather than being matched by a time
window, because an offline sale rung up during a shift but synced after it
closed would fall outside any window and vanish from the reconciliation — and
that is precisely the sale a shop most needs accounted for. So a late arrival is
possible, and when one happens the shift's figures are left exactly as counted
and a `LATE_ATTRIBUTION` discrepancy is written instead.

This is the same principle as no PAID-to-VOID and append-only ledgers: a
close-time figure records what was true when a person was accountable for it,
not a running total that drifts as more data arrives. Recomputing would mean two
different correct answers exist for "what was shift X's variance", depending on
when you ask.

The cost is that a shift's total and the sales total will not tie out without
reading both. That is accepted deliberately. Every `ShiftDiscrepancy` carries a
foreign key to its shift, so joining frozen figures to what landed afterwards is
a query rather than an investigation whenever the reporting work happens.

### Who may close

Their own drawer, always. Somebody else's only with manager rights — a cashier
closing a colleague's would be putting a figure against another person's name.
A manager doing it (the cashier went home) writes a `FORCED_CLOSE` discrepancy:
a normal thing that happens, and also exactly what it would look like if it were
not.

---

## Compliance

This is a **boundary**, not a KRA integration. Every sale carries what a
compliance regime needs, and the regime-specific part sits behind an adapter, so
a real eTIMS integration later is a new class rather than a rewrite of how a sale
is rung up.

### An invoice number is not a receipt number

Two separate series, and reusing one would be a mistake.

A receipt number identifies what a customer was handed. An invoice number
identifies a *taxable document*, and the two diverge immediately: a void never
gets an invoice number, a credit note gets one of its own, and a business not
registered for VAT gets receipt numbers and no invoices at all. Sharing a series
would put gaps in the tax sequence the moment anything was voided — and a
gapless tax sequence is precisely what a revenue authority looks at.

`InvoiceCounter` is per business, locked for each allocation, the same shape as
`ReceiptCounter`. Gaplessness is proved by a threaded test rather than a
sequential one, because a race is the only thing that can break it.

### Allocation is never deferred

The number is taken **inside the transaction that commits the sale**. An online
sale is numbered immediately, in its own transaction. An offline sale is
numbered when it syncs — the same code, running later, because syncing is when
that sale commits.

What must never exist is a mechanism that defers allocation for a sale that has
*already* committed. That would put the hole back.

### Offline sales have no invoice number until they land

Allocating on a disconnected till would make gaplessness across two tills
unenforceable, so an offline sale simply has no invoice number until it syncs.
The cost is that a customer asking for a tax invoice at a disconnected till
cannot have one on the spot.

**This is the position taken here, not a confirmed KRA requirement.** It needs
verification against real guidance before a VAT-registered client relies on it —
same caveat as the eTIMS scope note below.

### A number is taken only when something will be filed

`invoice_number` is **null** whenever nothing will be filed against the
document: a business under no regime, or an offline sale that has not synced.
Both are the same rule — putting either in the tax series would claim a filing
that will never happen, and the series is the thing a revenue authority reads.

An unregistered shop that is handed a buyer PIN still gets the document
recorded, unnumbered. A customer asked, and that request is worth a trace; it is
just not part of any return, so the export excludes it. Postgres treats nulls as
distinct, so the unique constraint on `(tenant, invoice_number)` still holds
however many there are.

### Settlement is read from the database, never from the caller

`issue_invoice` re-reads the sale rather than trusting the instance it was
handed. `take_cash` settles its own re-fetched row under a lock, so a caller's
object is routinely still `OPEN` with stale totals — a mistake the sync path
actually made and the checkout view avoided only by remembering to refresh.
Verifying at the boundary means a fourth call site cannot repeat it, and it also
stops stale totals being frozen onto a tax document.

### Documents are immutable

A `ComplianceDocument` is frozen once issued. A correction is a credit note
referencing the original, never an edit — the same discipline as append-only
ledgers, and here it is not only principle: an editable tax record is not a tax
record. `save()` refuses anything outside `MUTABLE_AFTER_ISSUE`, which is
submission bookkeeping only. A submission that succeeds on the third attempt has
not changed a single tax fact.

The tax breakdown is snapshotted per rate rather than derived on read, so a rate
change next year cannot restate what was declared. Per rate because a return is
filled in that way, and a duka selling zero-rated bread alongside 16% sugar
would otherwise leave the filer splitting a single total by hand.

### The adapter boundary

```python
class ComplianceAdapter(Protocol):
    name: str
    submits: bool
    def issue(self, document) -> ComplianceResult: ...
    def credit(self, document) -> ComplianceResult: ...
    def status(self, document) -> ComplianceResult: ...
```

Two implementations from the start, deliberately — one implementation does not
prove a boundary, it only describes one. Neither is a stub:

| Adapter | For |
|---|---|
| `NullAdapter` | A business not registered for VAT. **The common case**, not a placeholder. Documents are recorded and numbered; nothing is submitted, recorded as `NOT_REQUIRED` so "nothing to send" is distinguishable from "not sent yet". |
| `ManualExportAdapter` | A registered business with no gateway. Produces exactly what somebody would otherwise type into eTIMS Lite, as CSV and PDF. |

Both run against one conformance suite, so a third adapter cannot ship having
quietly redefined what `issue` means. A failure is a `ComplianceResult`, never
an exception — the far end is a government service over a Kenyan connection, and
an adapter that threw would take a sale down with it after the goods had left
the shop.

An unknown mode falls back to the null adapter rather than raising. A setting
that has drifted must stop a shop *submitting*, not stop it selling.

But a silent fallback means a registered business quietly stops filing and
nobody finds out until a return is due, so the fallback writes a
`COMPLIANCE_MODE_UNKNOWN` audit entry — one per affected document, deliberately
not deduplicated. Every mis-filed sale deserves its own record, and a condition
that should never occur is worth being noisy about when it does. Documents are
also listed in the platform console, where a run of `FAILED` submissions or a
stream of unnumbered documents is visible to the operator.

### The back office

| Endpoint | Gate | Why |
|---|---|---|
| `GET /compliance/settings/` | any staff | A till needs to know whether to ask for a buyer PIN |
| `PATCH /compliance/settings/` | **Owner** | Changing the regime decides whether the business declares tax at all. Wrong in one direction declares tax that is not owed; in the other, fails to declare tax that is. Both land on the owner. Audited as `COMPLIANCE_MODE_CHANGED` with old and new value and the actor. |
| `GET /compliance/export/` and `/export/pdf/` | Manager+ | The back office's own filing work; a manager doing the monthly return should not need the owner's account |
| `GET /compliance/documents/` | Manager+ | Read-only. A document is immutable, and an endpoint that appeared to edit one would lie about what the system does. |

The invoice prefix is frozen once the series has started — changing it mid-way
would produce two spellings of one gapless sequence, and a filer could not tell
whether anything was missing between them. The counter itself is read-only on
this surface: a counter that can be set by hand is not a gapless series.

The PDF is a **separate path**, not `?format=pdf`. DRF reserves `format` for
content negotiation and answers 404 on an unrecognised one, so a format
parameter here would look like a missing URL rather than a bad request. The
receipt endpoints are split for the same reason.

---

## Reporting

**Nothing here computes new truth.** Every figure is read from the ledgers that
already exist — sales, payments, refunds, shifts — because a report that
disagrees with the sale it came from is worse than no report. There is no
warehouse and no star schema: this reads the operational tables. A duka does a
few hundred sales a day, and a denormalised copy would add a synchronisation
problem, and a second place for figures to be wrong, to solve a performance
problem nobody has. If it stops being enough, the answer is materialised views
over the same tables, not a parallel schema.

One query layer in `apps/reports/queries.py`: pure functions taking
`(tenant, period)` and returning dataclasses. The API, the CSV, the PDF and the
platform summary all call it, so the four cannot disagree — the arrangement that
already keeps the receipt's two renderings honest.

### Four decisions behind every number

| Question | Answer |
|---|---|
| Which clock? | **`server_received_at`, always.** A till with a wrong clock must not move revenue between days. `device_created_at` is shown *on a sale*, never used to bucket one. |
| Which day boundary? | **The business's own**, from `Tenant.timezone`. A Nairobi shop trading at 10pm is already tomorrow in UTC; a report ending at 3am local is one nobody trusts twice. |
| Where do refunds land? | **The period they were issued in**, not the period of the sale they correct. Revenue for a closed month must not change retroactively — the same principle as a frozen shift close. The refund names its sale, so both stay visible. |
| Cash or all money? | **Both, separately.** Cash reconciles against a drawer somebody counted; M-Pesa against a statement somebody downloads. One combined number serves neither. |

Voided sales appear nowhere in revenue and are counted on their own — a rising
void count is a signal, and burying it inside a revenue query is how it goes
unnoticed. Periods are half-open (`start <= t < end`) so consecutive ones
neither overlap nor gap.

### Cashier performance is framed, not just computed

This is the report most likely to be misused, so the framing is part of the
design rather than left to whoever builds a screen.

A cashier on the quiet shift will always look worse per sale. A discount rate
says nothing without knowing *who authorised* each discount — and the authoriser
is recorded on the sale, not on the cashier. So:

- Every rate is returned **beside the counts it comes from**, in the API and in
  the CSV. A reader can see how thin the evidence is.
- Somebody who worked and sold nothing **does not appear**. An absence is not a
  zero, and a row of zeroes against a name reads as a judgement the data does
  not support.
- The response carries a `note` saying exactly this, so the framing travels with
  the data rather than living only in a screen somebody might rebuild
  differently.

The purpose is to find a pattern worth asking about, not to rank people.

### The report-versus-drawer tie-out

A manager compares "today's sales" against "the drawer" and finds they differ.
**Both numbers are right.** A shift's closing figures are frozen at the moment
somebody counted and signed for them; a sale that synced afterwards is filed
against that shift without touching them.

The answer is to **show both, clearly separated** — never to merge them into a
recomputed total, which would give two different correct answers to "what was
this shift's variance" depending on when you asked.

`apps/reports/drawer.py` returns, per shift:

- `counted` — float, declared, expected, variance. Exactly as signed for.
- `arrived_after_close` — the late payments, with the sales they belong to.
- `explained_variance_cents` — what the variance *would* have read as. An
  explanation of the gap, **not a correction to it**; the variance itself never
  moves.

The late arrivals are found through `ShiftDiscrepancy`'s `LATE_ATTRIBUTION`
rows rather than by comparing timestamps, because the discrepancy is the
*record* that something arrived late, written by the code that knew at the time.
A timestamp comparison would re-derive it and quietly disagree the first time a
definition moved. This is what that foreign key was added for one milestone
earlier.

M-Pesa never appears in the late-cash figure: it was never in the drawer, so it
cannot explain a cash gap.

### The platform usage summary

Two views, deliberately separate. `platform/usage/` counts **structure** —
staff, branches, tills, catalogue size — and carries no money; it predates there
being any sales to count. `platform/trading/` counts **what was sold** in a
period, which is what a usage-based invoice keys on.

This is the one place a reporting bug becomes a *billing* bug, so it gets what
money gets: integer cents, its own tests, and a `bypass_rls()` window kept to
the queries themselves — no view logic, no serialization, nothing else runs
with isolation off.

**Suspended businesses are included.** One suspended halfway through a month
still traded for the part before and still owes for it; dropping it would
quietly forgive an invoice with nobody able to notice which. Businesses that
traded nothing are included at zero, because an absence and a zero are different
facts to an invoicing run.

### Reports are online-only

The till app deliberately does not cache aggregates. A report is regenerable;
the outbox exists for money that must not be lost. Showing a manager stale
figures with no indication they were stale is worse than showing nothing.

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
- **eTIMS integration.** The compliance adapter interface and a manual/export
  implementation are built. A real OSCU/VSCU integration plugs into that
  interface later without touching the sales schema. Nothing here talks to KRA,
  holds credentials, or carries a device certificate.
