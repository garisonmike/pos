import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/api_client.dart';
import 'core/session_store.dart';
import 'data/auth_repository.dart';
import 'data/cart/checkout_service.dart';
import 'data/catalog_repository.dart';
import 'data/models.dart';
import 'data/outbox/database.dart';
import 'data/outbox/outbox_repository.dart';
import 'data/outbox/pin_lockout.dart';
import 'data/printing/escpos.dart';
import 'data/reports/reports_repository.dart';

/// Where the API lives.
///
/// Overridden at launch from a `--dart-define`, so a demo build can point at a
/// deployed server without a code change. The default is the Android emulator's
/// alias for the host machine, which is where the API runs during development.
const defaultBaseUrl = String.fromEnvironment(
  'POS_API_URL',
  defaultValue: 'http://10.0.2.2:8000',
);

final baseUrlProvider = Provider<String>((ref) => defaultBaseUrl);

final sessionStoreProvider = Provider<SessionStore>((ref) => SessionStore());

final apiClientProvider = Provider<ApiClient>(
  (ref) => ApiClient(
    baseUrl: ref.watch(baseUrlProvider),
    session: ref.watch(sessionStoreProvider),
  ),
);

final authRepositoryProvider = Provider<AuthRepository>(
  (ref) => AuthRepository(
    api: ref.watch(apiClientProvider),
    session: ref.watch(sessionStoreProvider),
  ),
);

final catalogRepositoryProvider = Provider<CatalogRepository>(
  (ref) => CatalogRepository(ref.watch(apiClientProvider)),
);

/// The till's own database.
///
/// One instance for the life of the app. Opening a second connection to the
/// same file would let two writers disagree about the outbox, which is the one
/// table where a lost row is lost money.
final outboxDatabaseProvider = Provider<OutboxDatabase>((ref) {
  final db = OutboxDatabase();
  ref.onDispose(db.close);
  return db;
});

final pinLockoutProvider = Provider<PinLockout>(
  (ref) => PinLockout(ref.watch(outboxDatabaseProvider)),
);

final outboxProvider = Provider<OutboxRepository>(
  (ref) => OutboxRepository(
    database: ref.watch(outboxDatabaseProvider),
    transport: ApiSyncTransport(ref.watch(apiClientProvider)),
    lockout: ref.watch(pinLockoutProvider),
  ),
);

final checkoutServiceProvider = Provider<CheckoutService>(
  (ref) => CheckoutService(
    transport: ApiCheckoutTransport(ref.watch(apiClientProvider)),
    outbox: ref.watch(outboxProvider),
  ),
);

/// Reports and drawers.
///
/// Online-only by design: a report is regenerable, and caching aggregates would
/// show a manager stale figures with no indication they were stale. The
/// repository raises ReportsUnavailable rather than degrading to an empty
/// report, and the screens render that as its own state.
final reportsRepositoryProvider = Provider<ReportsRepository>(
  (ref) => ReportsRepository(ApiReportsTransport(ref.watch(apiClientProvider))),
);

/// Where receipts are printed.
///
/// An in-memory transport until a Bluetooth printer is paired. A till with no
/// printer must still be able to sell - the receipt is the one part of a sale
/// that can wait.
final printerProvider = Provider<PrinterTransport>((ref) => InMemoryPrinter());

/// Which of the three states this till is in.
///
/// The router reads this and nothing else, so "where should the app be" lives
/// in one place rather than being decided by each screen.
enum TillStage {
  /// No business chosen yet. Someone must say which shop this till belongs to.
  unclaimed,

  /// The business is known but the till is not registered, so there is no
  /// device token and PIN sign-in cannot work. A manager signs in with a
  /// password once to get past this.
  claimed,

  /// Registered, so a cashier can take over with four digits.
  registered,

  /// A cashier is signed in.
  signedIn,
}

class TillState {
  const TillState({required this.stage, this.session, this.tenantSlug, this.tenantName});

  final TillStage stage;
  final Session? session;
  final String? tenantSlug;
  final String? tenantName;
}

/// Works out the till's state at launch, and after every sign-in or sign-out.
class TillStateNotifier extends StateNotifier<AsyncValue<TillState>> {
  TillStateNotifier(this._ref) : super(const AsyncValue.loading()) {
    refresh();
  }

  final Ref _ref;

  Future<void> refresh() async {
    state = const AsyncValue.loading();
    final store = _ref.read(sessionStoreProvider);

    final slug = await store.tenantSlug();
    if (slug == null) {
      state = const AsyncValue.data(TillState(stage: TillStage.unclaimed));
      return;
    }

    final name = await store.tenantName();

    if (await store.hasAccessToken()) {
      try {
        final session = await _ref.read(authRepositoryProvider).currentSession();
        state = AsyncValue.data(
          TillState(
            stage: TillStage.signedIn,
            session: session,
            tenantSlug: slug,
            tenantName: session.tenantName,
          ),
        );
        return;
      } on ApiException catch (error) {
        // Offline with a stored token is not a reason to sign anyone out - the
        // till may simply be between connections, and throwing a cashier back
        // to sign-in mid-shift over a dropped bar of signal would be worse than
        // trusting what is stored.
        if (error.isOffline) {
          state = AsyncValue.data(
            TillState(
              stage: TillStage.signedIn,
              tenantSlug: slug,
              tenantName: name,
            ),
          );
          return;
        }
        await store.clear();
      }
    }

    state = AsyncValue.data(
      TillState(
        stage: await store.isDeviceRegistered()
            ? TillStage.registered
            : TillStage.claimed,
        tenantSlug: slug,
        tenantName: name,
      ),
    );
  }

  Future<void> signOut() async {
    await _ref.read(authRepositoryProvider).signOut();
    await refresh();
  }
}

final tillStateProvider =
    StateNotifierProvider<TillStateNotifier, AsyncValue<TillState>>(
      TillStateNotifier.new,
    );

/// Whether this till has been registered, which decides if PIN sign-in is on.
final deviceRegisteredProvider = FutureProvider<bool>(
  (ref) => ref.watch(sessionStoreProvider).isDeviceRegistered(),
);

/// The cashier who last signed in, to pre-fill the PIN screen.
final lastUsernameProvider = FutureProvider<String?>(
  (ref) => ref.watch(sessionStoreProvider).lastUsername(),
);

final categoriesProvider = FutureProvider<List<Category>>(
  (ref) => ref.watch(catalogRepositoryProvider).categories(),
);

/// The category currently filtering the catalogue; null means everything.
final selectedCategoryProvider = StateProvider<String?>((ref) => null);

/// The current search text.
final searchQueryProvider = StateProvider<String>((ref) => '');

/// The catalogue as the browse screen should show it.
///
/// Searching and browsing are one provider rather than two screens, because to
/// the person holding the till they are the same activity: find the thing.
final visibleItemsProvider = FutureProvider<List<Item>>((ref) async {
  final repository = ref.watch(catalogRepositoryProvider);
  final query = ref.watch(searchQueryProvider);

  if (query.trim().length >= 2) {
    return repository.search(query);
  }
  return repository.items(categoryId: ref.watch(selectedCategoryProvider));
});
