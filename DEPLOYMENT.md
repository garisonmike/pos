# Deploying

One server, Docker Compose, Caddy in front. This is written to be followed
top to bottom on a fresh machine.

Two steps in here are not optional and are easy to skip because everything
appears to work without them: the [proxy verification](#verify-the-proxy-hop-count)
and the [restore drill](#the-restore-drill). Both prove something that is
otherwise only believed.

---

## What runs

```
internet ──► caddy ──► gunicorn ──► postgres
   :80/:443   (TLS)      (api)   └─► redis
                                 └─► backup
```

### Only Caddy binds a host port

**Postgres has no published port. Neither does Redis, and neither does
gunicorn.** All three are reachable on the compose network and from nowhere
else. `docker-compose.prod.yml` removes each development port mapping
explicitly with `!reset`, because Compose *merges* `ports` across files rather
than replacing them — leaving one out would silently keep the development
mapping and publish a database to the internet.

Confirm it on the running deployment rather than trusting this paragraph:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml config | grep -A2 ports:
```

The only `published:` entries should be `80`, `443` and `443/udp`, all under
`caddy`. And from outside the server:

```bash
nc -zv YOUR_SERVER_IP 5432   # must fail
nc -zv YOUR_SERVER_IP 6379   # must fail
nc -zv YOUR_SERVER_IP 8000   # must fail
```

This is not tidiness. The M-Pesa IP allowlist reads the **last**
`X-Forwarded-For` entry and trusts it, which is only sound if exactly one proxy
can reach the application. A published gunicorn port would let anybody bypass
Caddy and write their own header.

---

## Before you start

These need your account or your money, and are tracked in `input.md`:

- a server (2GB RAM is enough for several shops)
- a domain, with an A record pointing at it
- the secrets in `.env.production`, generated fresh
- somewhere off-site for the backups to go

---

## First deployment

### 1. Get the code onto the server

```bash
git clone git@github.com:garisonmike/pos.git
cd pos
```

### 2. Fill in the environment

```bash
cp .env.production.example .env.production
chmod 600 .env.production
$EDITOR .env.production
```

Every line marked `CHANGE ME` must be changed. Generate secrets on the server,
not on your laptop:

```bash
# DJANGO_SECRET_KEY, POSTGRES_PASSWORD, DB_PASSWORD, REDIS_PASSWORD,
# PLATFORM_ADMIN_PASSWORD
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# DARAJA_ENCRYPTION_KEY - must be a Fernet key, not a random string
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Copy `DARAJA_ENCRYPTION_KEY` somewhere off this server before continuing.**
Losing it means every tenant's stored M-Pesa credentials become undecryptable,
and there is no recovery — each shop would have to re-enter them.

### 3. Point DNS at the server

Caddy requests a certificate on first start and needs the domain to already
resolve. Check before starting:

```bash
dig +short YOUR_DOMAIN     # must return the server's IP
```

### 4. Start it

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The API container applies migrations and seeds the platform admin on start, so
there is no separate migrate step.

### 5. Collect static files

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec api python manage.py collectstatic --no-input
```

Needed on the first deploy and after any upgrade that changes admin assets.

### 6. Check it is up

```bash
curl -sS https://YOUR_DOMAIN/api/v1/health/
```

If Django is misconfigured it will not have started at all — the boot checks
(`pos.E001` through `pos.E004`) refuse to run with the default admin path, the
development signing key, or an unset proxy hop count.

---

## Verify the proxy hop count

**Do this on every deployment, and again after anything changes about what
sits in front of the application.**

`TRUSTED_PROXY_HOPS=1` says exactly one trusted proxy stands between the
internet and Django. The M-Pesa IP allowlist depends on it: it reads the last
`X-Forwarded-For` entry, because the leftmost ones are written by the caller
and anybody can put anything there.

The value is correct *because* of the topology — Caddy is the only service
binding a host port, and it appends exactly one entry. That reasoning is worth
nothing unless it is checked against the actual deployment.

Send a request with a forged header from outside the server:

```bash
curl -sS -H "X-Forwarded-For: 1.2.3.4" https://YOUR_DOMAIN/api/v1/health/
```

Then read what the application saw:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml logs --tail=20 caddy
```

**The client IP must be your real address, not `1.2.3.4`.** Caddy appends the
peer it actually saw, so the forged value ends up at the left where it is
ignored.

If you see `1.2.3.4`, the hop count does not match reality. Raise
`TRUSTED_PROXY_HOPS` by the number of proxies you have added and check again.
Until it passes, treat the M-Pesa IP allowlist as not working.

---

## Backups

The `backup` service runs nightly at `BACKUP_HOUR` (default 02:00 container
time) and writes to the `backups` volume: a `pg_dump -Fc` of the database and a
tarball of the media directory, keeping 7 daily and 4 weekly copies.

The dump is verified as it is taken — an empty file, or one `pg_restore
--list` cannot parse, is deleted and reported as a failure rather than kept.

Run one by hand:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup /scripts/backup.sh
```

### Everything the backup does not cover

- **`.env.production`** — secrets are not in the dump, deliberately. Keep a
  copy somewhere safe and off this server.
- **The `caddy_data` volume** — TLS certificates and ACME state. Losing it
  means re-issuing, which Let's Encrypt rate-limits.
- **Off-site copies.** The nightly dump lives on the same disk as the database
  it came from. A server whose backups live only on itself has no backups. See
  `input.md`.

---

## The restore drill

**A backup nobody has restored is a hope with a filename.**

Run this after the first deployment, and once a quarter afterwards. It restores
the most recent dump into a scratch database and counts what came back. It
never touches the live database.

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup /scripts/restore.sh --drill
```

Expected output:

```
Dump:   /backups/daily/pos-YYYYMMDD-HHMMSS.dump
Taken:  ...
Size:   ...

--- Live now (pos) ---
  tenants_tenant           3
  accounts_user            11
  catalog_item             240
  sales_sale               1876
  sales_payment            1902
  compliance_document      412

--- Restored from dump (pos_restore_test) ---
  tenants_tenant           3
  accounts_user            11
  catalog_item             240
  sales_sale               1874
  sales_payment            1900
  compliance_document      412

Sales:   live 1876, restored 1874
Tenants: live 3, restored 3

DRILL PASSED. The dump restores and contains the expected tables.
```

**What "matching" means here.** The restored copy is expected to be slightly
*behind* live — the dump was taken hours ago and the shop kept trading. What
would be wrong is the restored copy having *more* than live, having zero
tenants, or a table failing to come back at all. The script fails on those.

### Restoring for real

Only when something has actually gone wrong. This replaces every row.

```bash
# Stop the application first, so nothing writes while the restore runs.
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml stop api

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup sh -c 'RESTORE_I_MEAN_IT=yes /scripts/restore.sh --live'

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml start api
```

The script refuses without `RESTORE_I_MEAN_IT=yes`, and prints row counts
before and after so the damage is visible rather than assumed.

Media is restored separately:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup sh -c 'tar -xzf /backups/daily/media-STAMP.tar.gz -C /srv/media'
```

---

## Upgrading

```bash
git pull
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec api python manage.py collectstatic --no-input
```

Migrations run automatically on container start. **Take a backup first** —
`exec backup /scripts/backup.sh` — because a migration is the one change that
cannot be undone by redeploying the previous image.

---

## Reading the logs

```bash
# everything
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml logs -f

# just the backup service - this is where a failed nightly run says so
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml logs -f backup
```

A failed backup prints `!!! BACKUP FAILED !!!` to stderr. Nothing watches for
that yet — alerting is an open item in `input.md`, and until it exists the
backup log is something a person has to read.

---

## What is deliberately not here

- **No CI/CD.** Deploys are `git pull` and `up -d --build`, run by a person who
  can see the result.
- **No secrets manager.** One `.env.production` file, `chmod 600`. A vault is
  the right answer at more than a handful of servers; at one it is another
  thing to keep running.
- **No horizontal scaling.** One API container. Postgres and the single
  gunicorn are a long way from being the limit for shops doing a few hundred
  sales a day.
