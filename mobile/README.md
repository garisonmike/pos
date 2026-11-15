# POS Till

Android-first Flutter till for the POS Platform. The app supports business and
device setup, password and PIN sign-in, catalogue browsing, cash checkout,
receipt printing, offline sale sync, shift management, and sales reports.
Restaurant order-taking is not available in the app yet; restaurant order and
kitchen workflows are currently exposed through the backend API.

## Run locally

Start the API and database from the repository root with `docker compose up`.
Then run the till with Flutter 3.50.0 or newer (Dart 3.10 or newer):

```bash
flutter pub get
dart run build_runner build
flutter run
```

Android release builds use the NDK selected by the Flutter SDK; the sqlite3 JNI
plugin uses the same version. Ensure the Android SDK installs that NDK.

The nightly GitHub workflow makes an unsigned APK only as a compile smoke test.
For a signed distribution APK, push a version tag (`v*`) or manually run the
**Android release APK** GitHub Actions workflow. It builds the APK on GitHub,
verifies its signature, and uploads it as a 30-day workflow artifact; it does
not build or sign the production APK on this machine.

Before running it, configure these repository or environment Actions secrets:

- `POS_ANDROID_KEYSTORE_BASE64`: base64 encoding of the established production
  keystore.
- `POS_ANDROID_KEYSTORE_PASSWORD`
- `POS_ANDROID_KEY_ALIAS`
- `POS_ANDROID_KEY_PASSWORD`

Keep a protected backup of the established keystore and its passwords outside
the repository. Reuse the key that owns the app's existing signing identity;
do not create a replacement key for an app already distributed to devices.
The workflow never falls back to the debug signing key and fails if a secret
is missing. The decoded keystore is temporary to that GitHub runner and is
removed after the job.

The generated Drift outbox code is required and is not committed. Re-run the
build runner after changing files in `lib/data/outbox/`. The Android emulator
uses `10.0.2.2` to reach the API on the host. For a physical device, set the
host machine's LAN address instead:

```bash
flutter run --dart-define=POS_API_URL=http://192.168.1.20:8000
```

The local outbox is encrypted with a random key held in platform secure
storage. On first launch after this upgrade, the app verifies an encrypted
copy of any existing plaintext database before replacing it. If startup reports
a database recovery problem, do not uninstall the app or clear its data; the
queued sales may still be recoverable from that device.

An owner can open **More till actions → Till data recovery** to record the
64-character recovery key and save an encrypted database backup through
Android's document picker to a client-controlled location. Keep the key in a
password manager or other secure place separate from the backup. On a
replacement till, choose **Restore an encrypted backup** before setting up the
business, select the matching business file, and enter the key. The app checks
the key, database integrity, queued-sales table, and current schema before
replacing local data. Anyone with both the backup and key can read the till's
cached data and sales.

Backups are manual snapshots, not continuous replication. Make a new backup
after offline trading when the selected storage is reachable. Sales made after
the last saved snapshot cannot be restored if the original till is destroyed.
Changing this till to a different business clears its local cache and rotates
the recovery key; that action is blocked while unsynced/refused sales or
offline refusal reports remain.
The server supports a separate, revocable offline approval code. The till now has
a separate discount approval prompt, refreshes/caches the verifier, enforces a
local attempt lockout and sends only the verifier version when the sale syncs.
The recovery changes were verified with Flutter 3.50.0: analysis passed, all
241 Flutter tests passed, and an unsigned Android release APK built. The Android
file-picker workflow has not been exercised end to end on a physical device.
The APK remains unsigned until the customer release pipeline supplies its
signing key.

See the repository [README](../README.md) for backend setup and the full
platform overview.
