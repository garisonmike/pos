/// The till's own database.
///
/// A duka's connection drops for hours at a time, so the till is not a client
/// that caches - it is a shop's record of its own trading day that happens to
/// upload. Four tables, and each exists for a reason that has bitten somebody:
///
/// * [QueuedSales] - sales rung up with no connection, and what the server
///   eventually said about each one. Rows are deleted only on a verdict, never
///   on a hopeful assumption that the upload worked.
/// * [PinAttempts] - the lockout counter, kept here because the server's one
///   lives in Redis and Redis is exactly what is unreachable. Without this, an
///   offline till would let somebody try a manager's four digits all evening.
/// * [SyncCursor] - the server timestamp the last catalogue download returned,
///   so the next one asks for a window the server chose rather than one this
///   tablet's clock invented.
/// * [CatalogCache] - the price list and the staff list, flattened for pricing
///   a cart with nothing to ask.
library;

import 'package:drift/drift.dart';
import 'package:drift_flutter/drift_flutter.dart';

part 'database.g.dart';

/// What the server said about a queued sale, once it has said anything.
class SyncStatus {
  /// Not yet sent, or sent and no answer yet. Keep it and retry.
  static const pending = 'pending';

  /// Written by the server for the first time. Safe to delete locally.
  static const accepted = 'accepted';

  /// The server already had it. Also safe to delete - a replay is a success,
  /// not an error, and treating it as one would leave rows here forever.
  static const duplicate = 'duplicate';

  /// The server refused it and retrying unchanged will not help. Kept and
  /// shown to a person: this is money that was taken and has no home yet, and
  /// deleting it quietly would be the worst thing this app could do.
  static const rejected = 'rejected';
}

/// Sales rung up while the till had no connection.
@DataClassName('QueuedSale')
class QueuedSales extends Table {
  /// Generated here, on the till, at the moment of sale. This is the
  /// idempotency key the server dedupes on, so it is generated once and never
  /// regenerated on retry - regenerating it is precisely how one sale becomes
  /// two.
  TextColumn get clientUuid => text()();

  /// Monotonic per till. A gap in the series is visible to the shop, which is
  /// how a wiped tablet or a hand-edited database gets noticed.
  IntColumn get deviceSequence => integer()();

  /// The whole request body, as it will be sent. Stored serialised rather than
  /// as columns because the payload's shape belongs to the API, and a schema
  /// migration here every time a field is added would be a second place to get
  /// the contract wrong.
  TextColumn get payloadJson => text()();

  /// The till's clock at the moment of sale. Sent for the shop's own reading of
  /// the day; the server files it under its own clock for reporting.
  DateTimeColumn get deviceCreatedAt => dateTime()();

  TextColumn get status => text().withDefault(const Constant(SyncStatus.pending))();

  /// How many times an upload has been attempted, for backing off and for
  /// showing a person that something is stuck rather than merely slow.
  IntColumn get attempts => integer().withDefault(const Constant(0))();
  DateTimeColumn get lastAttemptAt => dateTime().nullable()();

  /// The server's refusal, kept verbatim so the person looking at a stuck sale
  /// reads what the server actually said.
  TextColumn get failureCode => text().nullable()();
  TextColumn get failureDetail => text().nullable()();

  /// Filled in once the server has taken it, so the till can print a proper
  /// receipt for a sale that was rung up hours earlier.
  TextColumn get serverSaleId => text().nullable()();
  IntColumn get receiptNumber => integer().nullable()();

  @override
  Set<Column> get primaryKey => {clientUuid};
}

/// The offline lockout counter.
///
/// The server counts failed PIN attempts in Redis. Offline, Redis is the thing
/// that cannot be reached - so a till with no connection would happily accept
/// unlimited guesses at a manager's four digits, which is the one situation
/// where nobody is watching. The count lives here instead, and is enforced
/// before the local PIN check runs.
///
/// It is deliberately *not* reset by reconnecting. A lockout that could be
/// cleared by toggling aeroplane mode would not be a lockout.
@DataClassName('PinAttempt')
class PinAttempts extends Table {
  /// Who is being guessed at, scoped to this till: `"<username>"`. Scoped by
  /// name rather than by user id because an unknown username must be counted
  /// too - otherwise the counter itself would tell an attacker which names
  /// exist.
  TextColumn get scopeKey => text()();

  IntColumn get failures => integer().withDefault(const Constant(0))();

  /// Milliseconds since the epoch, or null when not locked. Stored as an
  /// integer rather than a timestamp so that a device clock rolled backwards
  /// still compares, and checked against a monotonically sourced now.
  IntColumn get lockedUntil => integer().nullable()();

  DateTimeColumn get lastFailureAt => dateTime().nullable()();

  /// Every refusal, queued to be sent home on the next sync so the shop's
  /// audit trail contains the failures and not only the successes. Serialised
  /// as a JSON list of `{username, reason_code, occurred_at}`.
  TextColumn get pendingTelemetryJson =>
      text().withDefault(const Constant('[]'))();

  @override
  Set<Column> get primaryKey => {scopeKey};
}

/// Where the last catalogue download left off.
@DataClassName('SyncCursorRow')
class SyncCursor extends Table {
  /// One row per thing being tracked; currently just `'catalog'`. A table
  /// rather than a single value so that a second stream can be added without a
  /// migration.
  TextColumn get name => text()();

  /// The `server_time` the last successful download returned, stored as the
  /// exact string the server sent. Never this device's clock: a till running
  /// fast would ask for a window that skips changes it never saw.
  TextColumn get serverTime => text().nullable()();

  DateTimeColumn get lastSyncedAt => dateTime().nullable()();

  @override
  Set<Column> get primaryKey => {name};
}

/// The price list, flattened for pricing a cart with nothing to ask.
@DataClassName('CachedItem')
class CatalogCache extends Table {
  TextColumn get id => text()();
  TextColumn get name => text()();
  TextColumn get sku => text().withDefault(const Constant(''))();

  /// A JSON list. An item can carry several - a shop's own label and the
  /// manufacturer's - and a single column would force a choice about which
  /// barcode "counts", leaving the other to fail at the scanner.
  TextColumn get barcodesJson => text().withDefault(const Constant('[]'))();

  TextColumn get unit => text().withDefault(const Constant('pc'))();
  IntColumn get priceCents => integer()();
  BoolColumn get isPriceVariable =>
      boolean().withDefault(const Constant(false))();
  IntColumn get taxRateBps => integer().withDefault(const Constant(0))();
  BoolColumn get taxIsInclusive =>
      boolean().withDefault(const Constant(false))();

  /// Withdrawn items are kept with the flag cleared rather than deleted, so
  /// that a till which was offline when something was withdrawn stops selling
  /// it as soon as it syncs - and so a queued sale that already includes it
  /// still has a name to print.
  BoolColumn get isActive => boolean().withDefault(const Constant(true))();

  DateTimeColumn get updatedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {id};
}

/// Staff, as far as this till needs to know them.
@DataClassName('CachedStaff')
class StaffCache extends Table {
  TextColumn get id => text()();
  TextColumn get username => text()();
  TextColumn get fullName => text()();
  TextColumn get role => text()();

  /// Empty for anyone who cannot authorise a discount - the server does not
  /// send a cashier's hash at all, because no offline check would consult it.
  TextColumn get pinHash => text().withDefault(const Constant(''))();

  /// Which version of that PIN this cached row is. Sent back with any offline
  /// approval made against it, so the server can tell that a till approved
  /// something against a PIN that has since been changed or revoked. A
  /// counter - it carries nothing derived from the PIN itself.
  IntColumn get pinVersion => integer().withDefault(const Constant(0))();

  BoolColumn get isActive => boolean().withDefault(const Constant(true))();
  DateTimeColumn get updatedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {id};
}

@DriftDatabase(
  tables: [QueuedSales, PinAttempts, SyncCursor, CatalogCache, StaffCache],
)
class OutboxDatabase extends _$OutboxDatabase {
  OutboxDatabase([QueryExecutor? executor])
      : super(executor ?? driftDatabase(name: 'pos_till_outbox'));

  @override
  int get schemaVersion => 1;
}
