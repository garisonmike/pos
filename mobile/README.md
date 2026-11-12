# POS Till

Android-first Flutter till for the POS Platform. The app supports business and
device setup, password and PIN sign-in, catalogue browsing, cash checkout,
receipt printing, offline sale sync, shift management, and sales reports.
Restaurant order-taking is not available in the app yet; restaurant order and
kitchen workflows are currently exposed through the backend API.

## Run locally

Start the API and database from the repository root with `docker compose up`.
Then run the till with a recent Flutter SDK:

```bash
flutter pub get
dart run build_runner build
flutter run
```

The generated Drift outbox code is required and is not committed. Re-run the
build runner after changing files in `lib/data/outbox/`. The Android emulator
uses `10.0.2.2` to reach the API on the host. For a physical device, set the
host machine's LAN address instead:

```bash
flutter run --dart-define=POS_API_URL=http://192.168.1.20:8000
```

See the repository [README](../README.md) for backend setup and the full
platform overview.
