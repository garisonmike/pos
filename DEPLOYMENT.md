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

Arrange these deployment-specific items before starting:

- a server (2GB RAM is enough for several shops)
- a domain, with an A record pointing at it
- the secrets in `.env.production`, generated fresh
- somewhere off-site for the backups to go

---

## First deployment

### 1. Get the code onto the server

```bash
git clone https://github.com/garisonmike/pos.git
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

Start Postgres first:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d db
```

Reconcile the runtime account before starting the API. This also handles an
existing Postgres volume, where initialization scripts are not rerun:

```bash
scripts/reconcile-db-role.sh --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml
```

This removes the test runner's former `CREATEDB` privilege from the runtime
role. New databases initialize the runtime role without that privilege; the
development/CI-only test role is omitted from production Compose.

Then start the application:

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

### Client-specific staff setup: offline approval codes

Each manager who must approve discounts while a till is disconnected needs a
separate offline approval code. Sign-in PINs are not used for this and are no
longer included in catalog sync.

An owner can provision or rotate a manager's code through the authenticated
user-management API:

```http
POST /api/v1/auth/users/{manager_id}/rotate-offline-approval/
Authorization: Bearer <owner access token>
```

The response contains the code and its version. The code is shown once; store
it through the business's agreed secure credential process and give it only to
that manager. Repeating the request rotates the code again. The till receives
only a SHA-256 verifier for an enrolled manager during catalog sync. The raw
code is never sent with a sale. Use
`POST /api/v1/auth/users/{manager_id}/revoke-offline-approval/` to revoke it
without changing that person's sign-in PIN. Deactivation and downgrading below
manager also revoke it.

The till now has a separate offline discount prompt and refreshes the staff
verifier cache before approval when connected. The Android release build
compiles; exercise the file-picker recovery flow on the target device before
handing it to a client. A rooted or modified device can still fabricate an
offline authorization payload. Review offline sales and revoke a lost till.


### Client-specific till recovery setup

After signing in as the owner, open **More till actions → Till data recovery**.
Record the 64-character recovery key in the client's password manager or other
approved secure store, separately from the backup destination. Select the
client-controlled storage provider in Android's file picker when saving an
encrypted snapshot. Set an operational cadence that covers offline trading;
the app displays the last snapshot time on that till but does not upload
automatically.

To recover, install the signed app on a replacement till, choose **Restore an
encrypted backup** before business setup, select the file for the correct
business, and enter the separately stored key. The import verifies the key,
SQLite integrity, queued-sales table, and current schema before replacing the
local database. A recovery snapshot cannot contain sales made after it was
created; the client must accept and manage that recovery point objective.

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
time; startup rejects hours outside 0–23 and non-positive retention values)
and writes to the `backups` volume: a `pg_dump -Fc` of the database and a
tarball of the media directory, keeping 7 daily and 4 weekly copies. Media
backup is required by default: if the configured media mount is absent, the
backup fails instead of publishing a database-only copy. Set
`MEDIA_BACKUP_REQUIRED=false` only for an installation that intentionally
stores no uploaded media; such backups print an explicit warning.

The dump is verified as it is taken — an empty file, or one `pg_restore
--list` cannot parse, is deleted and reported as a failure rather than kept.

### Configure encrypted off-site copies

Production requires an off-site destination. The backup container uses rclone,
and refuses to start unless its destination is an encrypted `crypt` remote and
an HTTPS alert endpoint is configured. Rclone encrypts file contents and, with
the required settings, both file and directory names before sending data to the
provider. See the [rclone crypt documentation](https://rclone.org/crypt/) for
the encryption model and provider setup.

On an administrator's trusted machine:

1. Create an rclone remote for the selected object-storage provider.
2. Create a `crypt` remote over a dedicated path in that provider. Use
   standard filename encryption and directory-name encryption.
3. Encrypt the rclone config file with [`rclone config encryption set`](https://rclone.org/commands/rclone_config_encryption_set/)
   and retain its password separately. Keep the config file outside the
   repository with mode `0600`.
4. Copy the encrypted config to the deployment host at the path set by
   `BACKUP_RCLONE_CONFIG_FILE` (default `./.secrets/rclone.conf`). Set
   `BACKUP_RCLONE_CONFIG_PASSWORD`, `BACKUP_REMOTE` (for example
   `encrypted:pos-production`), and an HTTPS `BACKUP_ALERT_URL` that accepts a
   plain-text POST, such as an ntfy topic.
5. Configure provider lifecycle retention and access controls. The job also
   prunes encrypted daily copies older than 30 days and weekly copies older
   than 365 days by default; both periods are configurable.

The job uploads database and media archives, downloads each object through the
decrypting remote, and compares SHA-256 digests before reporting success. It
sends success and failure notifications. The backup container health check
goes unhealthy when no completed backup exists within 25 hours; connect that
health state to an external uptime monitor so a stopped container or missed
schedule also pages someone. The application cannot create an object-storage
account, alert receiver, lifecycle rule, or external monitor on the owner's
behalf; those deployment choices remain setup work.

To drill a remote copy, first copy a matching database and media pair back into
the backup volume. The crypt remote decrypts them on download:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup sh -c 'mkdir -p /backups/remote-drill && \
    rclone copyto "$BACKUP_REMOTE/daily/pos-STAMP.dump" \
      /backups/remote-drill/pos-STAMP.dump && \
    rclone copyto "$BACKUP_REMOTE/daily/media-STAMP.tar.gz" \
      /backups/remote-drill/media-STAMP.tar.gz && \
    /scripts/restore.sh --drill /backups/remote-drill/pos-STAMP.dump'
```

Replace both `STAMP` values with the same timestamp from the backup filenames.
Keep the drill output and check that it reports `DRILL PASSED` before calling
the remote recovery path proven.

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
- **Off-site restore drill.** The production job sends encrypted copies to the
  configured remote. A deployment still has to restore a remote copy into a
  scratch database and verify media before it can claim its off-site recovery
  path works.

---

## The restore drill

**A backup nobody has restored is a hope with a filename.**

Run this after the first deployment, and once a quarter afterwards. It restores
the most recent local dump into a scratch database, checks that core tables are
queryable, reports their restored row counts as a limited sanity check, and
extracts the matching media archive into a temporary directory to verify that
every archived entry can be restored. It never touches the live database or
media volume. Counts are not compared with live: later edits or deletions can
make a valid older snapshot smaller, and row counts do not prove completeness.

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

--- Restored from dump (pos_restore_test) ---
  tenants_tenant           3
  accounts_user            11
  catalog_item             240
  sales_sale               1874
  sales_payment            1900
  compliance_document      412

Rows accounts_user: restored 11 (sanity check only).
Rows catalog_item: restored 240 (sanity check only).
Rows sales_sale: restored 1874 (sanity check only).
Rows sales_payment: restored 1900 (sanity check only).
Rows compliance_document: restored 412 (sanity check only).
Media:   archive read and restored (NN entries).
DRILL PASSED. The dump restores and the matching media archive was read.
Row counts are limited sanity checks; they do not prove snapshot completeness.
```

**What "matching" means here.** The database dump and media archive share the
same timestamp and retention directory. The drill verifies that the dump can
be restored, core tables can be queried, and the archive can be extracted. It
does not establish that every expected database row or media file is present;
use an independent backup manifest or recovery review when completeness needs
to be proven.

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

A failed backup prints `!!! BACKUP FAILED !!!` to stderr, and that event is
also sent to the configured alert endpoint. The backup container health check
reports a stale or absent successful run; connect it to an external uptime
monitor to alert on missed runs and API outages.

---

## Outage response

1. Check `docker compose ps` and the API/backup health states. Read the API and
   backup logs before restarting anything; preserve the request ID when
   reporting an API failure.
2. If the API or network is unavailable but the till is still operating,
   continue only within the till's offline workflow. Do not clear app data,
   reinstall the till, or reset its database while its outbox contains pending
   or rejected sales.
3. When service returns, let the till sync and review every rejected sale or
   discrepancy before treating the books as reconciled. Replaying a sale is
   idempotent; manually entering it again can create a separate sale.
4. For suspected database loss, stop writes and follow the live restore
   procedure. Prefer an off-site copy if the host or its backup volume is
   unavailable. Run a scratch restore drill before declaring service recovered.
5. Record the start/end time, affected stores/tills, lost service, recovery
   source, and unresolved sales. The deployment owner must configure the
   customer-facing support contact and outage-notification channel before
   onboarding shops.

---

## Tenant export and customer exit

For an export request, verify the request with the business owner and obtain a
customer-controlled `age` public recipient through a separate trusted channel.
Never ask the customer to send the matching private identity key. The export
contains tenant settings, staff and operational records, sales and payment
history, audit records, and uploaded media. It omits operator-only tenant
notes, account password/PIN/offline-approval hashes, device token hashes,
M-Pesa credentials, and callback tokens. The archive manifest lists its records, media, and
redacted fields.

Run the export on the API container, then copy only the encrypted artifact to
the approved transfer location:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec api python manage.py export_tenant \
    --tenant-id TENANT_UUID \
    --actor-username PLATFORM_ADMIN_USERNAME \
    --recipient age1CUSTOMER_PUBLIC_RECIPIENT \
    --output /tmp/tenant-export.age

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  cp api:/tmp/tenant-export.age ./tenant-export.age

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec api rm -f /tmp/tenant-export.age
```

The command requires an existing platform administrator, writes no plaintext
export to disk, uses a consistent read-only tenant snapshot, records the export
and encrypted artifact digest in that tenant's audit history, and refuses to
overwrite an existing artifact. The customer
can decrypt it with their private identity and inspect the ZIP manifest:

```bash
age --decrypt --identity customer-identity.txt \
  --output tenant-export.zip tenant-export.age
unzip -l tenant-export.zip
```

Send the encrypted file only through the agreed customer channel, confirm
receipt, and record the handoff and deletion. An export does not delete or
anonymize records. Apply the organization's approved statutory retention
schedule before any later data deletion; the retention period and deletion
authorization must be decided for the service before onboarding.

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
