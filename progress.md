# Progress log

Newest entry on top. Each entry: what I worked on, what I finished, decisions I
made and why, and anything I need to come back to.

---

## 2026-08-15 — The compliance layer

**Worked on:** milestone 4. Invoice numbering, the immutable document, the
adapter boundary and the manual export.

**Finished:** `InvoiceCounter`, `ComplianceDocument`, `NullAdapter`,
`ManualExportAdapter`, the shared conformance suite, CSV and PDF export, and
102 tests.

**Decisions, and why:**

**A separate invoice series.** A receipt number identifies what a customer was
handed; an invoice number identifies a taxable document. They diverge
immediately - a void never gets an invoice number, a credit note gets its own,
and an unregistered business gets receipts and no invoices at all. Sharing one
series would gap the tax sequence the moment anything was voided, and that
sequence is exactly what a revenue authority reads.

**Allocation is never deferred.** The number is taken inside the transaction
that commits the sale. Online that is immediate; offline it happens at sync,
which is the same code running later because syncing is when that sale commits.
What I was careful *not* to build is a mechanism that defers allocation for a
sale that has already committed - that would put the hole back.

**Two adapters from the start, and neither is a stub.** One implementation
describes a boundary; two prove it. `NullAdapter` is the common case - most
small dukas are not VAT-registered - and `ManualExportAdapter` is what a
registered business without a gateway actually does. Both run against one
conformance suite, so a third cannot ship having quietly redefined `issue`.

**A failure is a result, not an exception.** The far end is a government
service over a Kenyan connection. An adapter that threw would take a sale down
with it after the goods had left the shop.

**The tax breakdown is per rate.** A single total tells a filer nothing when a
sale mixes zero-rated bread with 16% sugar, which a duka does constantly.
Snapshotted rather than derived, so a rate change next year cannot restate what
was declared.

**One thing found while building:** the sync path passed a stale in-memory sale
to the invoicing hook. `take_cash` settles its own re-fetched row under a lock,
so the instance the caller holds is still `OPEN` and the invoice was refused.
The checkout view already refreshed before calling; sync did not. Caught by the
offline-invoicing tests, which is what they were for.

**Flagged, and written into ARCHITECTURE.md next to the offline-invoice
position:** that position is mine, not a confirmed KRA requirement. It needs
checking against real guidance before a VAT-registered client relies on it.
Same caveat as the eTIMS scope note.

**Come back to:** a settings endpoint for compliance mode and prefix, and an
export download endpoint. The layer works; there is no back-office surface on
it yet.

---

## 2026-08-14 — Shifts, the cash drawer, and closing milestone 3

**Worked on:** shift open/close with drawer reconciliation, then the milestone
close-out.

**Confirmed before starting, as asked:** whether the four cash-rounding
regression tests actually assert settlement. I did not take my own word for it -
I reverted the fix and ran them. Three of four failed; one passed. The weak one
was the *rounded-up* case: overpaying by the rounding pushed the ledger past the
raw total, so the sale reached PAID by accident while being wrongly marked
overpaid, and a test checking only the state and the adjustment sailed through.
I strengthened it to assert the ledger is exactly covered, added a fifth test
for the rounding-down side, and re-ran against the reverted code: all five now
fail. Then restored the fix and confirmed all five pass.

That is the lesson the original bug taught, so it is worth naming: every figure
on the rounded-down path was already correct while the bug was live - the amount
due, the payment, the overpaid flag. The only thing wrong was that the sale sat
at OPEN forever. Ledger arithmetic alone would never have caught it.

**Finished:** `Shift`, `CashMovement`, `DrawerCount`, `ShiftDiscrepancy`, the
open/close/cash endpoints, and 53 tests.

**Decisions, and why:**

**The count is blind, and it is enforced at the API rather than in the
interface.** No endpoint reports what an open drawer is expected to hold. A
cashier shown the figure first is not counting, they are typing a number back,
and the control becomes theatre - which is worse than no control because it
looks like one. Three tests check the expectation is null on `current`, on the
detail view and in the listing.

**Cash only.** An M-Pesa payment never touched the drawer. Folding it in is how
a till reads twenty thousand short every day until nobody trusts the
reconciliation. `attribute_payment` skips non-cash entirely, so an M-Pesa
callback landing after a drawer closed raises nothing - flagging it would be
noise about money that was never in the till.

**Closing figures are frozen, per the decision on late attribution.** A payment
names its shift explicitly rather than being matched by a time window, because
an offline sale rung up during a shift but synced after it closed would fall
outside any window and vanish from the reconciliation - exactly the sale a shop
most needs accounted for. So late arrivals happen, and when one does the shift's
figures stay as counted and a `LATE_ATTRIBUTION` discrepancy is written with an
FK back to the shift.

**A variance never blocks the close**, and carries the full breakdown - float,
cash sales, refunds, paid in, paid out. A cashier who is nine hundred short
needs to see which line it should have come from.

**A denomination breakdown that does not sum to the declared total is refused
rather than corrected.** Picking a winner between two disagreeing figures would
hide which one the cashier got wrong.

**Shifts are optional.** A duka with one person and one drawer may never open
one. `Payment.shift` is nullable and the whole feature is inert if unused,
which is also why adding it did not disturb any existing test.

**Milestone 3 closed out:** README, ARCHITECTURE, CHANGELOG 0.3.0, tasks.md and
this log all brought current.

**Left undone, and worth being plain about:** shifts are server-side only. The
models, the reconciliation and the endpoints are built and tested, but there is
no cashier screen for opening or closing a drawer - a shop would have to drive
it through the API, which is not a shop feature. Recorded in the CHANGELOG's
known limitations and in tasks.md rather than left to be discovered.

**Come back to:** those screens, and the offline approval code tracked under
account management. The latter is the one real security gap this milestone
leaves open, and it wants its own credential surface rather than being bolted
onto sync.

---

## 2026-08-14 — The selling screens, printing, and a live bug found underneath

**Worked on:** the undercharge fix the last entry flagged, then the till side of
selling.

**Finished:**

- Sync now settles an undercharged sale instead of refusing it.
- Cart, tender pad and finished-sale screens, with the offline indicator.
- ESC/POS printing, byte building kept pure.
- The two-tills-sold-the-last-unit case, end to end.

**A real bug, found while fixing the undercharge case.**

Working out how the shortfall should interact with cash rounding, I found that
`ledger_position` read `total_cents` raw while `take_cash` charged the
*rounded* figure. They disagreed by up to fifty cents, so **any** cash sale
whose total was not a whole shilling took the customer's money and never
settled: no receipt number, no stock movement, sale left at `OPEN`. With
VAT-inclusive pricing that is most sales. I confirmed it against the live
checkout endpoint before believing it.

`initiate_stk` had been adding the adjustment back by hand at its own call
site, which is what a missing concept looks like from the inside. Settlement is
now decided against `collectable_cents = total + rounding - written_off`, and
the local patch is gone.

**Decisions, and why:**

**The shortfall is its own quantity, not a reduced total.** Folded into the
total it would be indistinguishable from a cheaper sale, and a till running a
stale price list for a month would never be spotted. `take_cash`'s
`insufficient_tender` guard is untouched for a live sale, where a cashier
holding too few notes genuinely has not finished.

**The device prices carts a second time, and a fixture the server generates
holds the two together.** I did not want two implementations of the same
arithmetic, but the alternative is telling a cashier there is no total until
the network comes back. So `gen_pricing_fixture.py` prices 120 awkward carts on
the server and the Dart test asserts every figure to the cent. It matched on
the first run.

**`client_uuid` is generated before the online attempt, not after it fails.**
The normal failure here is a request that hangs and succeeded invisibly. A
fresh identifier at queue time would make the sync create a second sale for
money taken once.

**A refusal is not a failure to connect.** A 403 proves the server is reachable,
so it must not put the till into the offline state and send a cashier hunting
for a network fault that is not there. Refusals keep the cart so the cashier can
fix them while the customer is still at the counter; only connectivity-shaped
failures and 5xx are queued.

**Three defects my own tests caught, worth recording:**

- Long item names were not truncated at all, so any real product name wrapped
  every row on the paper.
- The tender screen overflowed on a short viewport, which would have put the
  keypad off-screen on a small tablet.
- `Money.format(currency: '')` emitted a leading space, pushing every bare
  figure one character out of alignment in a column.

**Come back to:** shift open/close with cash drawer reconciliation - planned
separately, since it is new territory and it counts cash.

---

## 2026-08-14 — Offline sync

**Worked on:** the sync endpoints, the till's outbox database, and the offline
discount authorisation question I left open at the end of milestone 2.

**Finished:**

- `POST /api/v1/sync/sales/` — batch ingest with a verdict per sale. A till
  emptying its outbox needs to know exactly which rows it may delete, and one
  bad sale in a batch of forty must not strand the other thirty-nine.
- `GET /api/v1/sync/catalog/?since=` — delta pull on a cursor the server chose.
- Offline PIN lockout in the till's own database, with the same thresholds as
  the server.
- `pin_version` on `User`, and the fingerprint comparison at sync.
- Five drift tables on the device: `queued_sales`, `pin_attempts`,
  `sync_cursor`, `catalog_cache`, `staff_cache`.

**Decisions, and why:**

**Idempotency is the constraint's job, not a lookup's.** The obvious shape —
look for an existing sale, create one if there is none — passes every
sequential test and still sells the same bag of sugar twice, because two
threads both run the SELECT before either runs the INSERT. So `replay_sale`
inserts first and treats `IntegrityError` on
`unique_sale_client_uuid_per_tenant` as the duplicate signal. The test for it
uses real threads against `TransactionTestCase`, because the usual per-test
transaction hides the race completely — inside one transaction the threads
cannot see each other at all, so the bug being tested would not exist.

**The device check is a lookup, not a comparison.** A batch names a
`device_id`, and it is resolved inside the tenant's own scope. Another
business's device is simply not found. Writing it as
`device.tenant_id == tenant.id` would need the row first, and fetching the row
is the mistake.

**The PIN fingerprint: implemented, with the claim narrowed.** Every `set_pin`
and `clear_pin` bumps `pin_version`; the till caches it and sends it back with
an offline approval. This reliably catches a till approving against a PIN that
has since been changed or revoked. It does **not** prove the device performed
the check — anyone who can edit the till's local database can also read the
version it holds, so a fabricated payload built on a current cache carries a
matching version. I have written that limit into the docstring rather than
letting the comment imply a check that is not there. What does hold: staleness,
revocation, and a role re-check on the named authoriser.

**A stale authorisation never rejects the sale.** By the time sync runs the
customer has walked out with the goods. Refusing the record would delete the
evidence rather than the problem, so it lands flagged.

**Extracted `_resolve_store` to `apps/stores/selection.py`.** Three callers now.
A branch resolved one way at the till and another way at sync would file the
same sale's stock movement against different shops depending on whether the
network was up.

**Flagging, because it touches money and I would rather say it now — two things.**

**1. Fixed: a till that undercharged now settles instead of being refused.**
A price goes up while a till is disconnected, the till collects yesterday's
lower price, and at sync the cash is short. That used to be rejected, which
left the books without money the shop physically has and the stock on the shelf
in a system where it was not.

Sync now records the gap as `Sale.offline_shortfall_cents`, closes the ledger
against it and settles the sale `PAID` for what was collected, with stock
moving. `take_cash`'s `insufficient_tender` guard is untouched for a live sale,
where a cashier holding too few notes genuinely has not finished. An
`OFFLINE_SHORTFALL` discrepancy carries the amount and the reason; what the
shop does about the gap stays a human decision.

The shortfall is its own quantity, not a reduced total — folded into the total
it would look like a cheaper sale, and a till running a stale price list for a
month would never be spotted.

**And a real bug found underneath it.** Working out how the shortfall should
interact with cash rounding, I found that `ledger_position` read `total_cents`
raw while `take_cash` charged the *rounded* figure. They disagreed by up to
fifty cents, so **any** cash sale whose total was not a whole shilling took the
customer's money and never settled: no receipt number, no stock movement, sale
left sitting at `OPEN`. With VAT-inclusive pricing that is most sales. I
confirmed it against the live checkout endpoint before believing it.

`initiate_stk` had been adding the adjustment back by hand at its own call
site, which is exactly what a missing concept looks like from the inside.
Rounding is now part of `LedgerPosition`, settlement is decided against
`collectable_cents = total + rounding - written_off`, and the local patch is
gone. Four regression tests in the cash-checkout suite.

**2. A stolen till is a stolen manager PIN.** `GET /sync/catalog/` sends `pin_hash` down so an offline check has something to
verify against — managers only, never cashiers. But a PIN is four to six
digits, so a hash of one is brute-forceable by anyone holding the tablet, and
that same PIN works online. A stolen till is, in practice, a stolen manager
PIN. This is inherent to approving anything offline against a short secret.

The fix I want, and did not build here: a **separate offline approval code**,
versioned the same way, revocable on its own and useless for signing in. Then a
stolen till gives up only the thing that can be rotated without touching
anyone's sign-in. It needs its own credential-management surface — setting it,
rotating it, a screen for it — which is account management rather than sync, so
I have recorded it in ARCHITECTURE.md as open rather than widening this piece.

**Come back to:** ESC/POS printing, wiring the drift queue to a real cart and
checkout screen, the two-devices-sell-the-last-unit test, and shift open/close.

---

## 2026-08-13 — Milestone 2: catalogue, stock, import, and the first till screens

**Worked on:** everything after the close-outs — the catalogue, inventory, CSV
import, and the Flutter client.

**Finished:** 365 backend tests and 21 Flutter tests passing, `ruff` and
`flutter analyze` clean, and the retail template verified end to end through the
live API: onboard, set up, add a category and an item with two barcodes, resolve
the second barcode, take stock, refuse an adjustment with no reason, accept one
with a reason, see it appear in the low-stock list, import a CSV with a
deliberate bad row, and sign a cashier in by PIN who can search but not edit.

### Decisions worth sanity-checking

**Two flags on `Item` a retail-only build would have skipped.**
`is_available` is separate from `is_active` because "delisted" and "off today"
are different, and a salon needs "fully booked" as much as a duka needs "out of
season". `is_price_variable` is on from the start rather than retrofitted
through the cart in milestone 3. Both sit on `Item` rather than in a
product-only table — putting them there is precisely what would make services
second-class.

**Stock is a ledger with a cached total, not a number.** `StockItem.quantity`
can always be re-derived from the movements. It looks redundant until a count
disagrees with the shelf: the number says how many, only the ledger says why.

**Negative stock is allowed.** Refusing would mean refusing to record something
that has already happened. A visibly wrong number beats books that quietly
disagree with the drawer, so it is surfaced with a warning instead.

**`apply_movement` takes a row lock.** Two cashiers selling the last unit at
once would otherwise lose a movement to a classic lost update — which shows up
weeks later as stock that will not reconcile, not as an error anyone notices.

**Import is deliberately not atomic across rows.** You asked for per-row errors
over all-or-nothing, so a commit that hits bad rows still imports the good ones
*and* creates any categories they named. That means a partly-failed commit is
not a clean no-op. I think that is the right trade for onboarding, but it is the
one place where per-row handling has a visible cost, so flagging it plainly.

**The device token is stored as a credential, not a setting.** It looks like
configuration, but with four digits it signs someone in, so it lives in the
keystore. Signing out clears the session tokens and keeps it — which is exactly
what lets the next cashier take over with a PIN.

### Open question

Camera barcode scanning is not built; the till takes typed or
hardware-scanner input. Most Kenyan counters use a USB or Bluetooth scanner that
behaves as a keyboard, so this is the realistic path and the camera is a
convenience. Tell me if you want it in milestone 3 or later.

---

## 2026-08-13 — Milestone 2 close-outs: Redis, lockout, boot checks, and an isolation gap

**Worked on:** the three items carried over from milestone 1's sign-off, before
touching any catalog code.

**Finished:**

- Redis in the compose file, backing both the tenant status cache and the new
  lockout counters. Development now uses the same cache backend as production,
  because the lockout depends on counters being shared across processes and a
  local-memory cache would hide that difference until it mattered.
- PIN lockout with two counters: five failures on a device refuses it for
  fifteen minutes, and a second per-user counter catches attempts spread across
  several tills. Every failure goes to the audit trail. Scoped request throttles
  sit on top as a blunt outer limit.
- Startup checks that refuse to run with an unchanged platform console path or
  the development signing key, and warn about a per-process cache.
- One test file proving the cross-tenant read boundary in both directions, and
  an ARCHITECTURE section on how it works plus the migration/backfill escape
  hatch.

249 tests passing, up from 201.

### The isolation gap was worse than I flagged

I said the coverage test had missed two empty Django join tables. Checking
properly against the live catalogue, it had missed **five** tables — and one of
them was not harmless:

```
token_blacklist_outstandingtoken   user_id -> accounts_user   rls=f
  columns: id, token TEXT, created_at, expires_at, user_id, jti
```

That table stores the **encoded refresh token itself**, in a text column, next
to the id of the user it was issued to. With no policy on it, a query made while
one business was bound could have read another business's refresh tokens
verbatim — a credential disclosure, not metadata. The other four
(`token_blacklist_blacklistedtoken`, the two `PermissionsMixin` join tables, and
`django_admin_log`) were the harmless kind I originally described.

The cause is worth stating because it generalises: milestone 1's coverage test
looked for models with a `tenant` **column**. All five of these belong to a
business through a **foreign key** instead, and four of them are not declared
anywhere in this codebase — Django creates them itself, so a walk over ordinary
models never sees them.

Fixed with `enable_rls_via()`, which defines a row's visibility as its parent's
visibility rather than copying the tenant predicate, so the rule cannot drift
out of step. The coverage test now resolves ownership transitively over foreign
keys and includes auto-created models. Verified against a live database: bound
to one business, one of six refresh tokens is visible and none belong to anyone
else.

### Decisions worth sanity-checking

**An unregistered device token does not count towards lockout.** Counting it
would let anyone lock a till they cannot otherwise touch by sending rubbish
tokens, which turns a protection into a way to stop a shop trading. A wrong PIN
counts; a wrong token does not.

**Failed attempts are filed against the username string, with no user
attached.** At that point nobody has proved they are that person, and attaching
them would put someone else's guessing into an innocent cashier's history —
exactly the record a manager might later read as evidence against them.

**Locking a cashier follows them across tills; locking a till does not follow
the cashier.** Two independent dimensions. My first version of the device-scope
test contradicted its own comment by reusing one cashier on both tills, and
failed correctly.

### Open question

Nothing blocking. Next up is catalog code proper, starting with the `Item`
extensions.

---

## 2026-08-13 — Milestone 1: tenant and auth foundation

**Worked on:** the whole of milestone 1 — repository skeleton, Docker setup,
tenant isolation, data model, authentication, the platform console, tests and
documentation.

**Finished:**

- Docker Compose bringing up Postgres 18 and the API with a single
  `docker compose up`, including migrations and platform admin seeding on start.
- Tenant isolation: shared schema, `tenant_id` everywhere, Postgres Row-Level
  Security as the enforcement layer, plus a scoping manager and three test
  suites.
- Data model: `Tenant`, `TenantModule`, `Store`, `User`, `Device`, `Category`,
  `TaxRate`, `Item`, `Barcode`, `AuditLog`.
- Authentication: JWT with a tenant claim, password sign-in, device-bound PIN
  sign-in, separate platform operator sign-in, refresh, sign-out, role
  permissions.
- Platform surface: onboarding, suspend/reactivate, usage counts, and a
  hardened Django admin restricted to platform administrators.
- Setup wizard, guarded to run exactly once.
- README, ARCHITECTURE, CHANGELOG, tasks and this log.

### Decisions worth sanity-checking

**One database role, not two, and `FORCE ROW LEVEL SECURITY` instead.**
The plan called for a separate migration role so the application role would not
own the tables and could not read past its own policies. Building it, I found
`FORCE ROW LEVEL SECURITY` solves the same problem more robustly and with far
less machinery: it makes policies apply to the table owner too, so ownership
stops mattering. Two Django database aliases would also have complicated
`pytest-django`'s test database creation.

What I promised is still true — the role Django connects as is `NOSUPERUSER`
and `NOBYPASSRLS` — but it is delivered differently. The `postgres` superuser is
used once, at container init, to create that role and hand it ownership, then
never again. A test asserts the connected role has neither attribute, and
another asserts `FORCE` is set on every tenant table, because a missing `FORCE`
would leave every policy present, correct and completely inert.

**Isolation is lifted by URL prefix, not by inspecting the user.**
Working out whether a caller may bypass isolation would require reading the
user table, which is itself isolated — circular. Prefixes are known before any
query runs. The upshot is that the routes where isolation is off are exactly
two prefixes, both of which independently require `IsPlatformAdmin`, and a test
walks the URL conf to prove no route there is unguarded. Worth a look, since
this is the design's one deliberate hole.

**The whole request runs in one transaction.**
The tenant binding uses `set_config(..., is_local => true)`, which is
transaction-scoped. That scoping is the point: a non-local `SET` would persist
on a pooled connection and the next request — possibly another shop's — would
inherit it. `ATOMIC_REQUESTS` is off because the middleware opens the
transaction itself; letting Django also wrap the view would only add a
savepoint.

**Usernames are unique per business, not globally.**
Two shops can each employ a `mary`. This needs Django's `auth.E003` system
check silenced, which I would normally be wary of — the reasoning is recorded
in `settings/base.py` next to the silencing, and the ambiguity the check guards
against is handled by a custom `get_by_natural_key` that resolves only platform
administrators. Sign-in takes a business slug, which the till stores once.

**A four-digit PIN is only half a credential.**
PIN sign-in requires a registered device token as well, so possession of the
till is the other half and it cannot be used from an arbitrary client. This
still needs rate limiting, which I have added to tasks.md rather than done —
see the open question below.

**Suspension returns 402, not 403.**
So the Flutter client can show "contact your provider" rather than a generic
permission error. Status is cached for 60 seconds, which is the worst-case
delay before a suspension bites; the platform surface clears the cache on
change so the operator sees it immediately.

**Money decisions.** Integer cents everywhere with a test that fails the build
on any `FloatField`; half-up rounding rather than banker's; tax rates in basis
points; inclusive tax derived by subtraction so `net + tax == gross` always
holds. Cash rounds to the shilling since there is no coin below KES 1 in
practical use.

**Catalogue models landed in this milestone, CRUD did not.**
`Item`, `Category`, `TaxRate` and `Barcode` exist as tables with their RLS
policies, because the setup wizard has to create a default tax rate and because
getting every table's policy in place at once is safer than adding them
piecemeal. The endpoints for managing them are milestone 2.

### Two real bugs the tests caught, both worth knowing about

**Token refresh was broken for exactly the case it exists for.** Every request
carries its token in the `Authorization` header, which is where the middleware
looks to decide which business it may see. A refresh request carries the token
in the *body* — and by definition has no usable access token for the header,
since that is why it is refreshing. So the middleware bound nothing, isolation
refused the user lookup, and the endpoint returned a 500.

The symptom would have been a till going quiet for an hour and then being
unable to sign back in without a full password sign-in, which is the precise
situation refresh tokens exist to avoid. Fixed with a refresh view that reads
the tenant from the refresh token's own claim. There is now a test that
refreshes with no `Authorization` header at all.

**Cleanup that could not run left a stale tenant bound in the process.** When a
query inside `tenant_context()` failed, the cleanup that restores the previous
database setting ran its own query — on a transaction that was already aborted.
That raised, so the line resetting the in-process context variable never ran,
and the next piece of work inherited a tenant that no longer existed and wrote
rows pointing at it.

This one surfaced as forty-five unrelated test teardowns failing on a foreign
key, which took a while to trace back. The fix reverses the order — reset the
in-process binding first and unconditionally — and makes the database restore
skip a broken transaction, which is safe because the rollback discards the
setting anyway.

Both are the kind of bug that only appears when isolation is enforced by the
database. Worth flagging because they are the shape of thing to expect in later
milestones too.

### Discovered mid-build, added to tasks.md

- `FORCE ROW LEVEL SECURITY` is required, not optional — noted above.
- Per-business usernames need `auth.E003` silenced and a custom natural key
  lookup.
- Sign-in endpoints need rate limiting; PIN sign-in especially.
- Postgres 18 changed where its data directory lives; the compose mount had to
  move to `/var/lib/postgresql`.
- `DefaultRouter` adds an API root view with no permission classes. Harmless in
  tenant mode, not harmless behind the platform prefix where isolation is
  lifted. The URL-conf walking test caught it, which was reassuring — that test
  earned its place on its first run.
- Applying a business-type template only created missing module rows, so an
  owner picking a different type in the wizard did not get its modules. It now
  switches modules on, and never off, since it cannot tell a deliberate choice
  from a default.

### Open questions for you

1. **Rate limiting on sign-in.** A 4-digit PIN plus a valid device token is
   brute-forceable in about ten thousand attempts if nothing throttles it. I
   have not added throttling yet because it wants Redis, which is not in the
   compose file. Options: add Redis now and do it properly, use Django's
   database cache as an interim, or leave it until the production deployment
   task. My preference is Redis now, since the tenant status cache wants it too
   and adding a service later means changing the one documented setup step.

2. **`PLATFORM_ADMIN_URL` is committed in `.env.example`** as
   `ops-console-8f31c2/`. It is not a security boundary — authentication and
   the platform-admin check are — but its value being in a public repository
   removes what little obscurity it buys. Change it in your real `.env`, or say
   the word and I will generate it at first boot instead.

3. **No Flutter yet.** Milestone 1 is entirely backend. The client scaffold is
   in tasks.md as unscheduled; tell me whether you want it started alongside
   milestone 2's catalogue work or held until milestone 3 when there is a
   checkout to build.

### How to check it yourself

```bash
cp .env.example .env
docker compose up
docker compose exec api pytest
```

Then <http://localhost:8000/api/docs/> for the endpoints, and the README's
"Trying it out" section to onboard a shop and sign in as its owner from the
command line.
