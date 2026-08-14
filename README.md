# POS Platform

A multi-tenant point of sale system for small Kenyan businesses, deployed once
and used by many independent shops. Each business gets isolated data, its own
staff and roles, its own receipt branding, and only the features its trade
actually needs.

It is built for the hardware and the connectivity that small retailers really
have: an Android phone or tablet as the till, mobile data that drops out, and
M-Pesa alongside cash.

**Supported business types**

| Type | What it adds |
|---|---|
| Retail shop | Barcode or search checkout with stock tracking. The default. |
| Restaurant or bar | Orders held against a table, item modifiers, kitchen tickets. |
| Salon or services | Services with a duration and assigned staff, booked in advance. |
| Pharmacy | Retail plus batch numbers, expiry dates and expiry alerts. |

These are not four products. They are one product with different optional
modules switched on, sharing the same tenant, item and sale models. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how that is kept true.

---

## Current state

Milestones 1 and 2 are complete: tenant isolation, authentication, the platform
console, the catalogue, stock, CSV import, and an Android till that signs in and
browses.

Milestone 3 is most of the way there on the server. Cart arithmetic, the sale
state machine, cash checkout with a manager-authorised discount gate, M-Pesa
STK push with its callback and reconciliation, receipts as text and PDF, and
offline sync — batch ingest, idempotent replay and the till's outbox database —
are all built and tested. What is left is the till side of selling: wiring the
outbox to a real cart and checkout screen, and ESC/POS printing.

See [tasks.md](tasks.md) for what is done and what is next, and
[progress.md](progress.md) for a dated log of decisions.

---

## Setup from a clean machine

You need **Docker** and **Docker Compose**. Nothing else — no Python, no
Postgres, no virtualenv. Postgres runs in a container, and the API runs in a
container alongside it.

```bash
git clone git@github.com:garisonmike/pos.git
cd pos
cp .env.example .env
docker compose up
```

That is the whole setup. On first run it creates the database, creates the
application database role, applies migrations, seeds a platform administrator,
and starts the API on <http://localhost:8000>.

Wait for `Starting development server at http://0.0.0.0:8000/` and then:

| What | Where |
|---|---|
| API docs (Swagger) | <http://localhost:8000/api/docs/> |
| API docs (ReDoc) | <http://localhost:8000/api/redoc/> |
| OpenAPI schema | <http://localhost:8000/api/schema/> |
| Health check | <http://localhost:8000/api/v1/health/> |
| Platform console | <http://localhost:8000/ops-console-8f31c2/> |

Sign in to the console with `PLATFORM_ADMIN_USERNAME` and
`PLATFORM_ADMIN_PASSWORD` from your `.env` (`platform` /
`change-me-in-production` by default).

> There is deliberately no "run it without Docker" path. Tenant isolation
> depends on Django connecting as a specific non-superuser Postgres role, and
> setting that up by hand is easy to get subtly wrong in a way that silently
> disables every isolation guarantee in the system. One documented route means
> one route that is actually tested.

### Stopping and resetting

```bash
docker compose down            # stop, keep the data
docker compose down -v         # stop and wipe the database entirely
```

---

## Trying it out

Onboard a business and sign in as its owner, entirely from the command line.

```bash
# 1. Sign in as the platform operator
curl -s localhost:8000/api/v1/platform/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"platform","password":"change-me-in-production"}'

# 2. Onboard a shop (use the access token from step 1)
curl -s localhost:8000/api/v1/platform/tenants/ \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $PLATFORM_TOKEN" \
  -d '{
        "name": "Mama Njeri Duka",
        "business_type": "RETAIL",
        "owner_username": "njeri",
        "owner_full_name": "Njeri Kamau",
        "owner_password": "a-real-password-here"
      }'

# 3. Sign in as the shop owner
curl -s localhost:8000/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{
        "tenant_slug": "mama-njeri-duka",
        "username": "njeri",
        "password": "a-real-password-here"
      }'

# 4. Run first-time setup (use the owner's access token)
curl -s localhost:8000/api/v1/tenant/setup/ \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -d '{
        "business_type": "RETAIL",
        "vat_mode": "INCLUSIVE",
        "store_name": "Main shop",
        "store_code": "MAIN",
        "staff": [
          {"username": "mary", "full_name": "Mary Wanjiku",
           "password": "another-real-password", "pin": "1234", "role": "CASHIER"}
        ]
      }'
```

Mary can now sign in with her PIN from any till registered to this shop.

### Filling a catalogue

Onboarding is usually slowest at the product list, so there is a two-step CSV
import: check the file, then commit it.

```bash
# Get a template with every column and two worked examples
curl -s localhost:8000/api/v1/items/import/template/ \
  -H "Authorization: Bearer $OWNER_TOKEN" -o items.csv

# Check it. Nothing is written; you get a per-row report and a token.
curl -s localhost:8000/api/v1/items/import/validate/ \
  -H "Authorization: Bearer $OWNER_TOKEN" -F file=@items.csv

# Import the rows that passed
curl -s localhost:8000/api/v1/items/import/commit/ \
  -H "Authorization: Bearer $OWNER_TOKEN" -F file=@items.csv -F token=$TOKEN
```

Rows that fail come back with the row number, the field and what to do about it.
Valid rows still import — a file is rarely wrong all the way through.

---

## Running the app

The Flutter till lives in [mobile/](mobile/). It signs in, browses the
catalogue, and carries the offline outbox; the selling screens are next.

```bash
cd mobile
flutter pub get
dart run build_runner build        # generates the drift outbox code
flutter run                        # Android emulator reaches the API on 10.0.2.2
flutter test                       # widget and unit tests
flutter analyze
```

`build_runner` is a required step, not an optional one: the outbox database's
generated code is not committed, so the app will not compile without it. Re-run
it after changing anything in [mobile/lib/data/outbox/](mobile/lib/data/outbox/).

Pointing at a different API:

```bash
flutter run --dart-define=POS_API_URL=http://192.168.1.20:8000
```

On a physical device the emulator alias will not work — use the machine's LAN
address as above.

---

## Running the tests

```bash
docker compose exec api pytest     # backend
cd mobile && flutter test          # client
```

Run **one backend suite at a time**. Two concurrent `pytest` sessions fight over
the same test database and the second reports several hundred misleading errors
that have nothing to do with the code.

Useful variations:

```bash
docker compose exec api pytest -v                       # verbose
docker compose exec api pytest apps/core/tests/         # one area
docker compose exec api pytest -k isolation             # by name
docker compose exec api pytest --cov=apps --cov-report=term-missing
```

If the stack is not already running:

```bash
docker compose run --rm api pytest
```

### What the tests guard

Most of the suite is ordinary behavioural testing, but these exist
specifically to stop a class of bug from ever shipping:

| Test | Guards against |
|---|---|
| `test_rls_coverage.py` | A new model with a `tenant` column but no Row-Level Security policy. Fails the build rather than shipping a table that leaks. |
| `test_tenant_isolation.py` | Isolation failing at the database level, including a tenant binding surviving onto a reused pooled connection. |
| `test_cross_tenant_api.py` | Isolation failing at the API level — a view that looks a record up without scoping, or a payload that smuggles in another business's id. Also walks the URL conf to prove every platform route requires a platform administrator. |
| `test_money.py` | Rounding errors. Every edge case where a cent could appear or vanish. |
| `test_platform_read_boundary.py` | The one place a request may cross businesses. Asserts both directions at once, so neither isolation being off nor the console being broken can pass. |
| `test_pin_lockout.py` | A four-digit PIN being guessable. Proves attempts are bounded in total, not just in rate. |

The database role used by the tests is the same non-superuser, `NOBYPASSRLS`
role used in development and production. Running the isolation suite as a role
that can bypass isolation would make it pass while proving nothing.

---

## Environment variables

Copy `.env.example` to `.env`. Everything has a working development default;
everything marked below must be changed before a real deployment.

### Database

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_USER` | `pos_admin` | Superuser, used **only** to create the database and the application role on first boot. Django never connects with it. |
| `POSTGRES_PASSWORD` | — | **Change for deployment.** |
| `POSTGRES_DB` | `pos` | |
| `DB_USER` | `pos_app` | The role Django connects as. Created `NOSUPERUSER` and `NOBYPASSRLS` so Row-Level Security applies to it without exception. |
| `DB_PASSWORD` | — | **Change for deployment.** |
| `DB_NAME` / `DB_HOST` / `DB_PORT` | `pos` / `db` / `5432` | |

### Django

| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.dev` | `config.settings.prod` for deployment. |
| `DJANGO_SECRET_KEY` | insecure default | **Change for deployment.** |
| `DJANGO_DEBUG` | `true` | Must be `false` in production. |
| `DJANGO_ALLOWED_HOSTS` | localhost | Comma separated. |

### Authentication

| Variable | Default | Notes |
|---|---|---|
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` | `60` | Short, because signing out cannot revoke an already-issued access token. |
| `JWT_REFRESH_TOKEN_LIFETIME_DAYS` | `14` | Long enough that a till left over a weekend does not force a re-login. |

### Platform administration

| Variable | Default | Notes |
|---|---|---|
| `PLATFORM_ADMIN_URL` | `ops-console-8f31c2/` | Where the console is mounted. Deliberately not `admin/`. **Change it** so it is not the value published in this repository. |
| `PLATFORM_ADMIN_USERNAME` | `platform` | Seeded on first boot. |
| `PLATFORM_ADMIN_PASSWORD` | — | **Change for deployment.** Seeding is skipped if blank. |
| `PLATFORM_ADMIN_EMAIL` | | |
| `TENANT_STATUS_CACHE_SECONDS` | `60` | How long a business's active/suspended status is trusted before being re-read. This is the worst-case delay before a suspension takes effect. |

### Redis and till lockout

| Variable | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://cache:6379/0` | Holds suspension status and PIN lockout counters. Nothing is persisted; losing it costs one extra query per business. |
| `PIN_LOCKOUT_MAX_ATTEMPTS` | `5` | Consecutive failures before a till is refused. |
| `PIN_LOCKOUT_SECONDS` | `900` | How long the lockout lasts. |

> A four-digit PIN is guessable in ten thousand tries, so a rate limit alone is
> not enough — it caps how *fast* attempts arrive, not how many. The lockout caps
> the total. See ARCHITECTURE for why a wrong *device token* deliberately does
> not count towards it.

---

## Project layout

```
pos/
├── README.md              this file
├── ARCHITECTURE.md        system design and the reasoning behind it
├── CHANGELOG.md           what shipped, per milestone
├── tasks.md               living checklist, grouped by milestone
├── progress.md            dated log of work and decisions
├── docker-compose.yml     the one entry point
├── docker/postgres/init/  creates the non-superuser application role
├── mobile/                Flutter till (Android first)
│   └── lib/
│       ├── core/          theme, API client, session storage
│       ├── data/          models and repositories
│       └── features/      auth and catalogue screens
└── backend/
    ├── config/            settings, root URL conf
    ├── conftest.py        shared test fixtures
    └── apps/
        ├── core/          tenancy, money, auditing, permissions, errors
        ├── tenants/       businesses, modules, business-type templates
        ├── accounts/      users, roles, sign-in, registered tills
        ├── stores/        branches
        ├── catalog/       items, categories, tax rates, barcodes, CSV import
        ├── inventory/     stock levels and the movement ledger
        └── platform_admin/  provisioning, usage, the operator's console
```

Each app follows the same shape: `models.py`, `serializers.py`, `views.py`,
`services.py` for business logic that does not belong in a view, and `tests/`.

---

## Useful commands

```bash
docker compose logs -f api                          # follow API logs
docker compose exec api python manage.py shell      # Django shell
docker compose exec api python manage.py makemigrations
docker compose exec api python manage.py migrate
docker compose exec api ruff check apps config      # lint
docker compose exec db psql -U pos_admin -d pos     # database shell
```

To inspect the database from the host, Postgres is published on port **5433**
(not 5432, to avoid clashing with a local Postgres install):

```bash
psql -h localhost -p 5433 -U pos_admin -d pos
```

---

## Where to read next

- [ARCHITECTURE.md](ARCHITECTURE.md) — the system design, why each major
  choice was made, and full sections on tenant isolation and offline sync.
  Start with "Tenant isolation" if you only read one thing.
- [tasks.md](tasks.md) — what is built and what is next.
- [progress.md](progress.md) — dated decisions, newest first.
