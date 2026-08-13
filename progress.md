# Progress log

Newest entry on top. Each entry: what I worked on, what I finished, decisions I
made and why, and anything I need to come back to.

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
