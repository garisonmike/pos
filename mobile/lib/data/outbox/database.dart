/// The till's own database.
///
/// A duka's connection drops for hours at a time, so the till is not a client
/// that caches - it is a shop's record of its own trading day that happens to
/// upload. Four tables, and each exists for a reason that has bitten somebody:
///
/// * [QueuedSales] - sales rung up with no connection, and what the server
///   eventually said about each one. Rows are deleted only on a verdict, never
///   on a hopeful assumption that the upload worked.
/// * [PinAttempts] - the offline approval-code lockout counter, kept here
///   because the server's one lives in Redis and Redis is unreachable offline.
/// * [SyncCursor] - the server timestamp the last catalogue download returned,
///   so the next one asks for a window the server chose rather than one this
///   tablet's clock invented.
/// * [CatalogCache] - the price list and the staff list, flattened for pricing
///   a cart with nothing to ask.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:drift/drift.dart';
import 'package:drift_flutter/drift_flutter.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path_provider/path_provider.dart';
import 'package:sqlite3/sqlite3.dart' as sqlite;

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

  TextColumn get status =>
      text().withDefault(const Constant(SyncStatus.pending))();

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

/// The offline approval-code lockout counter.
///
/// The server counts failed sign-in PIN attempts in Redis. Offline, Redis is
/// unreachable, so the till keeps an additional local counter for approval
/// code attempts and enforces it before checking the local verifier.
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

  /// Empty until an owner provisions a separate offline approval code. The
  /// server never sends the short sign-in PIN verifier to a till.
  TextColumn get offlineApprovalHash =>
      text().withDefault(const Constant(''))();

  /// Which separate approval-code version this row contains. Sync compares it
  /// with the server copy to flag approvals made against a rotated code.
  IntColumn get offlineApprovalVersion =>
      integer().withDefault(const Constant(0))();

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
    : super(executor ?? _encryptedExecutor());

  static const _keyName = 'pos_till_outbox_key_v1';
  static const _recoveryKeyAcknowledgedName =
      'pos_till_outbox_recovery_key_acknowledged_v1';
  static const _lastRecoveryBackupName =
      'pos_till_outbox_last_recovery_backup_v1';
  static const _sqliteHeader = 'SQLite format 3\u0000';
  static const _databaseName = 'pos_till_outbox';
  static const _migrationBackupSuffix = '.plaintext-backup';
  static const _migrationTempSuffix = '.encrypting';
  static const _storage = FlutterSecureStorage();

  static String? _databaseKey;

  bool _closed = false;

  /// Avoid closing the background SQLite connection twice when a recovery
  /// export explicitly closes it and Riverpod disposes the provider later.
  Future<void> closeSafely() async {
    if (_closed) return;
    _closed = true;
    await close();
  }

  /// Forget local business data only after every queued/refused sale has a
  /// server-side outcome. A new business also gets a new database key so a
  /// former client's recovery key cannot decrypt its future sales.
  Future<void> resetForBusinessChange() async {
    final unresolved = await customSelect(
      'SELECT COUNT(*) AS count FROM queued_sales '
      'WHERE status IN (?, ?)',
      variables: [
        Variable<String>(SyncStatus.pending),
        Variable<String>(SyncStatus.rejected),
      ],
    ).get();
    final count = unresolved.single.data['count'] as int;
    final telemetryRows = await customSelect(
      'SELECT pending_telemetry_json FROM pin_attempts',
    ).get();
    final pendingRefusals = telemetryRows.fold<int>(
      0,
      (total, row) =>
          total +
          (jsonDecode(row.data['pending_telemetry_json'] as String) as List)
              .length,
    );
    if (count > 0 || pendingRefusals > 0) {
      throw StateError(
        '$count sale(s) and $pendingRefusals offline refusal record(s) still '
        'need to sync or be reviewed before changing business.',
      );
    }

    final file = await databaseFile();
    final oldKey = await _storage.read(key: _keyName);
    final oldAcknowledged = await _storage.read(
      key: _recoveryKeyAcknowledgedName,
    );
    final oldBackupTime = await _storage.read(key: _lastRecoveryBackupName);
    final originals = [
      for (final suffix in const [
        '',
        '-wal',
        '-shm',
        _migrationBackupSuffix,
        _migrationTempSuffix,
        '.restore-new',
        '.restore-previous',
        '.restore-previous-wal',
        '.restore-previous-shm',
      ])
        File('${file.path}$suffix'),
    ];
    final parked = [
      for (final original in originals)
        File('${original.path}.business-change-previous'),
    ];
    for (final candidate in parked) {
      if (await candidate.exists()) {
        throw StateError(
          'A previous business change needs review before changing business again.',
        );
      }
    }

    await closeSafely();
    final originallyPresent = <int>{};
    for (var i = 0; i < originals.length; i++) {
      if (await originals[i].exists()) originallyPresent.add(i);
    }
    final moved = <int>[];
    try {
      for (var i = 0; i < originals.length; i++) {
        if (originallyPresent.contains(i)) {
          await originals[i].rename(parked[i].path);
          moved.add(i);
        }
      }
      await _storage.delete(key: _keyName);
      await _storage.delete(key: _recoveryKeyAcknowledgedName);
      await _storage.delete(key: _lastRecoveryBackupName);
      _databaseKey = null;
      await initializeEncryptedStorage();
    } catch (_) {
      // Initialization might have created a partial fresh database. Remove
      // those files before putting the previous business back in place.
      for (var i = 0; i < originals.length; i++) {
        if (!originallyPresent.contains(i) || moved.contains(i)) {
          await _deleteIfPresent(originals[i]);
        }
      }
      for (final index in moved.reversed) {
        if (await parked[index].exists()) {
          await parked[index].rename(originals[index].path);
        }
      }
      await _restoreSecureValue(_keyName, oldKey);
      await _restoreSecureValue(_recoveryKeyAcknowledgedName, oldAcknowledged);
      await _restoreSecureValue(_lastRecoveryBackupName, oldBackupTime);
      _databaseKey = oldKey;
      rethrow;
    }

    // The new till has passed startup migrations and schema checks. The old
    // encrypted data can now be removed without risking a half-reset till.
    for (final candidate in parked) {
      await _deleteIfPresent(candidate);
    }
  }

  static Future<void> _restoreSecureValue(String name, String? value) async {
    if (value == null) {
      await _storage.delete(key: name);
    } else {
      await _storage.write(key: name, value: value);
      if (await _storage.read(key: name) != value) {
        throw StateError('Could not restore till recovery metadata.');
      }
    }
  }

  static Future<bool> needsFirstRunChoice() async {
    final directory = await getApplicationDocumentsDirectory();
    final file = File('${directory.path}/$_databaseName.sqlite');
    final previous = File('${file.path}.restore-previous');
    final plaintextBackup = File('${file.path}$_migrationBackupSuffix');
    final businessChangePrevious = File(
      '${file.path}.business-change-previous',
    );
    // A missing database can mean a fresh install or restored app settings
    // without app data. Always offer backup restore before making a new empty
    // outbox, even when Android restored the secure-storage key.
    return !await file.exists() &&
        !await previous.exists() &&
        !await businessChangePrevious.exists() &&
        !await plaintextBackup.exists();
  }

  static Future<File> databaseFile() async {
    final directory = await getApplicationDocumentsDirectory();
    return File('${directory.path}/$_databaseName.sqlite');
  }

  static Future<String> recoveryKey() async {
    final key = _databaseKey ?? await _storage.read(key: _keyName);
    if (key == null || !isValidRecoveryKey(key)) {
      throw StateError('The till recovery key is unavailable.');
    }
    return key;
  }

  static bool isValidRecoveryKey(String key) =>
      RegExp(r'^[0-9a-fA-F]{64}$').hasMatch(key);

  static Future<bool> recoveryKeyAcknowledged() async =>
      await _storage.read(key: _recoveryKeyAcknowledgedName) == 'true';

  static Future<void> acknowledgeRecoveryKey() =>
      _storage.write(key: _recoveryKeyAcknowledgedName, value: 'true');

  static Future<DateTime?> lastRecoveryBackupAt() async {
    final value = await _storage.read(key: _lastRecoveryBackupName);
    return value == null ? null : DateTime.tryParse(value)?.toLocal();
  }

  static Future<void> recordRecoveryBackup() => _storage.write(
    key: _lastRecoveryBackupName,
    value: DateTime.now().toUtc().toIso8601String(),
  );

  /// Replace the local database only after an encrypted copy has passed the
  /// current Drift migrations and its queued-sales table can be read. The
  /// previous file is retained until the restored file has passed the same
  /// check at its final location.
  static Future<void> restoreEncryptedBackup(File source, String key) async {
    if (!isValidRecoveryKey(key)) {
      throw const FormatException(
        'Recovery key must contain 64 hexadecimal characters.',
      );
    }

    final target = await databaseFile();
    final plaintextRecoveryCopy = File('${target.path}$_migrationBackupSuffix');
    if (await plaintextRecoveryCopy.exists()) {
      throw StateError(
        'A migration recovery copy is still present. Keep this till intact '
        'and contact support before restoring another database.',
      );
    }
    final temporary = File('${target.path}.restore-new');
    final previous = File('${target.path}.restore-previous');
    final targetWal = File('${target.path}-wal');
    final targetShm = File('${target.path}-shm');
    final temporaryWal = File('${temporary.path}-wal');
    final temporaryShm = File('${temporary.path}-shm');
    final previousWal = File('${previous.path}-wal');
    final previousShm = File('${previous.path}-shm');

    if (await previous.exists() ||
        await previousWal.exists() ||
        await previousShm.exists()) {
      throw StateError(
        'A previous recovery attempt needs review before another restore.',
      );
    }
    if (await temporary.exists()) await temporary.delete();
    await _deleteIfPresent(temporaryWal);
    await _deleteIfPresent(temporaryShm);
    await source.copy(temporary.path);
    try {
      await _verifyWithDrift(temporary.path, key);
    } catch (_) {
      if (await temporary.exists()) await temporary.delete();
      await _deleteIfPresent(temporaryWal);
      await _deleteIfPresent(temporaryShm);
      rethrow;
    }

    final oldKey = await _storage.read(key: _keyName);
    var movedCurrent = false;
    try {
      if (await target.exists()) {
        await target.rename(previous.path);
        movedCurrent = true;
      }
      if (await targetWal.exists()) await targetWal.rename(previousWal.path);
      if (await targetShm.exists()) await targetShm.rename(previousShm.path);
      await temporary.rename(target.path);
      await _storage.write(key: _keyName, value: key);
      if (await _storage.read(key: _keyName) != key) {
        throw StateError('Could not securely store the recovery key.');
      }
      _databaseKey = key;
      await _verifyWithDrift(target.path, key);
    } catch (_) {
      if (await target.exists()) await target.delete();
      if (await targetWal.exists()) await targetWal.delete();
      if (await targetShm.exists()) await targetShm.delete();
      if (movedCurrent && await previous.exists()) {
        await previous.rename(target.path);
      }
      if (await previousWal.exists()) await previousWal.rename(targetWal.path);
      if (await previousShm.exists()) await previousShm.rename(targetShm.path);
      if (oldKey == null) {
        await _storage.delete(key: _keyName);
      } else {
        await _storage.write(key: _keyName, value: oldKey);
      }
      _databaseKey = oldKey;
      rethrow;
    } finally {
      if (await temporary.exists()) await temporary.delete();
      await _deleteIfPresent(temporaryWal);
      await _deleteIfPresent(temporaryShm);
    }
    // Cleanup is deliberately outside the rollback block: a cleanup failure
    // must never delete the already verified restored database.
    await _deleteIfPresent(previous);
    await _deleteIfPresent(previousWal);
    await _deleteIfPresent(previousShm);
  }

  static Future<void> _deleteIfPresent(File file) async {
    try {
      if (await file.exists()) await file.delete();
    } on FileSystemException {
      // A retained encrypted previous copy is safe; it can be cleaned later.
    }
  }

  /// Prepare the key and migrate an existing plaintext outbox before providers
  /// can open the database. A failed migration leaves the original file in
  /// place and prevents the till from starting with an empty outbox.
  static Future<void> initializeEncryptedStorage() async {
    final directory = await getApplicationDocumentsDirectory();
    final path = '${directory.path}/$_databaseName.sqlite';
    final file = File(path);
    final backup = File('$path$_migrationBackupSuffix');
    final temp = File('$path$_migrationTempSuffix');
    final restorePrevious = File('$path.restore-previous');
    final businessChangePrevious = File('$path.business-change-previous');
    final restorePreviousWal = File('${restorePrevious.path}-wal');
    final restorePreviousShm = File('${restorePrevious.path}-shm');
    final restoreWal = File('$path-wal');
    final restoreShm = File('$path-shm');

    if (await businessChangePrevious.exists() && !await file.exists()) {
      // A process stopped during an intentional business reset. Do not make
      // an empty database while the previous business's encrypted data is
      // still parked; recovery needs an operator to choose the safe outcome.
      throw StateError(
        'A business change stopped before the new till was verified. '
        'Keep this till intact and contact support before continuing.',
      );
    }

    var key = await _storage.read(key: _keyName);
    if (!await file.exists() && await restorePrevious.exists()) {
      if (key == null) {
        throw StateError(
          'A recovery operation stopped before it could be verified. '
          'Keep this till intact and use its encrypted backup or support process.',
        );
      }
      _verifyEncryptedDatabase(restorePrevious.path, key);
      await restorePrevious.rename(path);
      if (await restorePreviousWal.exists()) {
        await restorePreviousWal.rename(restoreWal.path);
      }
      if (await restorePreviousShm.exists()) {
        await restorePreviousShm.rename(restoreShm.path);
      }
    } else if (await file.exists() && await restorePrevious.exists()) {
      var currentIsValid = false;
      try {
        if (key != null) _verifyEncryptedDatabase(path, key);
        currentIsValid = key != null;
      } on Object {
        // If the process stopped after moving the old file but before saving
        // the new key, recover the verified old copy rather than opening blank.
      }
      if (currentIsValid) {
        await restorePrevious.delete();
        if (await restorePreviousWal.exists()) {
          await restorePreviousWal.delete();
        }
        if (await restorePreviousShm.exists()) {
          await restorePreviousShm.delete();
        }
      } else if (key != null) {
        _verifyEncryptedDatabase(restorePrevious.path, key);
        await file.delete();
        if (await restoreWal.exists()) await restoreWal.delete();
        if (await restoreShm.exists()) await restoreShm.delete();
        await restorePrevious.rename(path);
        if (await restorePreviousWal.exists()) {
          await restorePreviousWal.rename(restoreWal.path);
        }
        if (await restorePreviousShm.exists()) {
          await restorePreviousShm.rename(restoreShm.path);
        }
      }
    }

    // Recover safely from a process interruption between renaming the old
    // file and installing the verified encrypted copy. Never promote a temp
    // file until the stored key has proved that it can be opened.
    if (!await file.exists() && await backup.exists()) {
      var recoveredEncryptedCopy = false;
      if (key != null && await temp.exists()) {
        try {
          _verifyEncryptedDatabase(temp.path, key);
          await temp.rename(path);
          _verifyEncryptedDatabase(path, key);
          recoveredEncryptedCopy = true;
        } on Object {
          // Keep the old database authoritative if the interrupted copy is
          // incomplete or cannot be verified.
          if (await file.exists() && await backup.exists()) {
            await file.delete();
          }
        }
      }
      if (!recoveredEncryptedCopy) {
        if (await temp.exists()) await temp.delete();
        await backup.rename(path);
      }
    }

    if (key == null) {
      if (await file.exists() && !await _isPlaintextSqlite(file)) {
        throw StateError(
          'The till database exists but its secure key is missing. '
          'Do not clear app data; contact support for recovery guidance.',
        );
      }
      key = _newDatabaseKey();
      await _storage.write(key: _keyName, value: key);
      // Read-after-write catches keystore failures before touching the outbox.
      if (await _storage.read(key: _keyName) != key) {
        throw StateError('Could not securely store the till database key.');
      }
    }

    _databaseKey = key;
    if (await file.exists() && await _isPlaintextSqlite(file)) {
      await migratePlaintextDatabase(
        file,
        temp,
        backup,
        key,
        retainBackupUntilSchemaReady: true,
      );
    } else if (await file.exists()) {
      _verifyEncryptedDatabase(path, key);
    }

    // Open once during startup to create/migrate the schema before screens
    // depend on it. A count query avoids loading the sales queue into memory.
    final database = OutboxDatabase();
    try {
      await database.customSelect('SELECT COUNT(*) FROM queued_sales').get();
    } finally {
      await database.closeSafely();
    }
    // Keep a plaintext source copy until encrypted storage has also passed
    // current Drift migrations and schema queries.
    await _deleteIfPresent(backup);
    await _deleteIfPresent(File('$path.restore-new'));
    for (final suffix in const [
      '',
      '-wal',
      '-shm',
      _migrationBackupSuffix,
      _migrationTempSuffix,
      '.restore-new',
      '.restore-previous',
      '.restore-previous-wal',
      '.restore-previous-shm',
    ]) {
      await _deleteIfPresent(
        File('$path$suffix.business-change-previous'),
      );
    }
  }

  static QueryExecutor _encryptedExecutor() {
    final key = _databaseKey;
    if (key == null) {
      throw StateError(
        'OutboxDatabase.initializeEncryptedStorage() must run before opening the till database.',
      );
    }

    return _executorForPath(null, key);
  }

  static QueryExecutor _executorForPath(String? databasePath, String key) {
    final path = databasePath;
    return driftDatabase(
      name: _databaseName,
      native: DriftNativeOptions(
        databasePath: path == null ? null : () async => path,
        setup: (rawDb) {
          final db = rawDb as sqlite.Database;
          if (db.select('PRAGMA cipher;').isEmpty) {
            throw StateError(
              'Encrypted SQLite support is missing from this build.',
            );
          }
          db.execute("PRAGMA key = '$key';");
          // Fail closed when the key does not unlock the database.
          db.select('SELECT count(*) FROM sqlite_master;');
        },
      ),
    );
  }

  static Future<void> _verifyWithDrift(String path, String key) async {
    if (await _isPlaintextSqlite(File(path))) {
      throw StateError('Recovery backups must be encrypted.');
    }
    _verifyEncryptedDatabase(path, key);
    final encrypted = sqlite.sqlite3.open(path);
    late int queuedBeforeMigration;
    try {
      encrypted.execute("PRAGMA key = '$key';");
      final names = encrypted
          .select("SELECT name FROM sqlite_master WHERE type = 'table'")
          .map((row) => row['name'] as String)
          .toSet();
      const requiredTables = {
        'queued_sales',
        'pin_attempts',
        'sync_cursor',
        'catalog_cache',
        'staff_cache',
      };
      if (!names.containsAll(requiredTables)) {
        throw StateError('The selected file is not a complete till backup.');
      }
      queuedBeforeMigration =
          encrypted
                  .select('SELECT COUNT(*) FROM queued_sales')
                  .single
                  .values
                  .single
              as int;
    } finally {
      encrypted.close();
    }

    final database = OutboxDatabase(_executorForPath(path, key));
    try {
      final queuedAfterMigration = await database
          .customSelect('SELECT COUNT(*) AS count FROM queued_sales')
          .get();
      await database.customSelect('SELECT COUNT(*) FROM catalog_cache').get();
      if (queuedAfterMigration.single.data['count'] != queuedBeforeMigration) {
        throw StateError('The restore changed the queued-sales count.');
      }
      await database.customStatement('PRAGMA wal_checkpoint(TRUNCATE)');
    } finally {
      await database.closeSafely();
    }
  }

  static String _newDatabaseKey() {
    final random = Random.secure();
    return List<int>.generate(
      32,
      (_) => random.nextInt(256),
    ).map((byte) => byte.toRadixString(16).padLeft(2, '0')).join();
  }

  static Future<bool> _isPlaintextSqlite(File file) async {
    final handle = await file.open();
    try {
      final header = await handle.read(16);
      return String.fromCharCodes(header) == _sqliteHeader;
    } finally {
      await handle.close();
    }
  }

  /// Encrypt an existing SQLite file without removing it until the new copy
  /// has been opened successfully with [key]. Kept public for migration tests.
  static Future<void> migratePlaintextDatabase(
    File source,
    File temp,
    File backup,
    String key, {
    bool retainBackupUntilSchemaReady = false,
  }) async {
    if (await temp.exists()) await temp.delete();
    if (await backup.exists()) await backup.delete();

    final tempPath = _quoteSqlString(temp.path);
    final plain = sqlite.sqlite3.open(source.path);
    try {
      plain.execute("VACUUM INTO '$tempPath';");
    } finally {
      plain.close();
    }

    final encryptedCopy = sqlite.sqlite3.open(temp.path);
    try {
      encryptedCopy.execute("PRAGMA rekey = '$key';");
    } finally {
      encryptedCopy.close();
    }
    _verifyEncryptedDatabase(temp.path, key);
    _verifyMigrationPreservedData(source.path, temp.path, key);
    if (await _isPlaintextSqlite(temp)) {
      throw StateError('The till database copy was not encrypted.');
    }

    await source.rename(backup.path);
    try {
      await temp.rename(source.path);
      _verifyEncryptedDatabase(source.path, key);
      if (!retainBackupUntilSchemaReady) await backup.delete();
    } catch (_) {
      if (await source.exists()) await source.delete();
      if (await backup.exists()) await backup.rename(source.path);
      rethrow;
    }
  }

  static void _verifyEncryptedDatabase(String path, String key) {
    final db = sqlite.sqlite3.open(path);
    try {
      if (db.select('PRAGMA cipher;').isEmpty) {
        throw StateError(
          'Encrypted SQLite support is missing from this build.',
        );
      }
      db.execute("PRAGMA key = '$key';");
      db.select('SELECT count(*) FROM sqlite_master;');
      final integrity = db.select('PRAGMA integrity_check;');
      if (integrity.length != 1 || integrity.single.values.single != 'ok') {
        throw StateError('Encrypted till database integrity check failed.');
      }
    } finally {
      db.close();
    }
  }

  static void _verifyMigrationPreservedData(
    String sourcePath,
    String encryptedPath,
    String key,
  ) {
    final source = sqlite.sqlite3.open(sourcePath);
    final encrypted = sqlite.sqlite3.open(encryptedPath);
    try {
      encrypted.execute("PRAGMA key = '$key';");
      final tableNames = source
          .select(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name;",
          )
          .map((row) => row['name'] as String)
          .toList();
      final encryptedTableNames = encrypted
          .select(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name;",
          )
          .map((row) => row['name'] as String)
          .toList();
      if (jsonEncode(tableNames) != jsonEncode(encryptedTableNames)) {
        throw StateError('Encrypted till database has a different table set.');
      }

      for (final table in tableNames) {
        final quoted = '"${table.replaceAll('"', '""')}"';
        final sourceCount = source
            .select('SELECT count(*) FROM $quoted;')
            .single
            .values
            .single;
        final encryptedCount = encrypted
            .select('SELECT count(*) FROM $quoted;')
            .single
            .values
            .single;
        if (sourceCount != encryptedCount) {
          throw StateError(
            'Encrypted till database row count changed for $table.',
          );
        }
      }

      // Row counts catch truncation, while these full row comparisons protect
      // the two non-rebuildable datasets: queued sales and offline refusal
      // telemetry. The app is not running during first-open migration.
      for (final tableAndKey in const {
        'queued_sales': 'client_uuid',
        'pin_attempts': 'scope_key',
      }.entries) {
        final table = tableAndKey.key;
        if (!tableNames.contains(table)) continue;
        final quotedTable = '"${table.replaceAll('"', '""')}"';
        final quotedKey = '"${tableAndKey.value}"';
        final sourceRows = source
            .select('SELECT * FROM $quotedTable ORDER BY $quotedKey;')
            .rows;
        final encryptedRows = encrypted
            .select('SELECT * FROM $quotedTable ORDER BY $quotedKey;')
            .rows;
        if (jsonEncode(sourceRows) != jsonEncode(encryptedRows)) {
          throw StateError('Encrypted till database changed rows in $table.');
        }
      }
    } finally {
      source.close();
      encrypted.close();
    }
  }

  static String _quoteSqlString(String value) => value.replaceAll("'", "''");

  /// Replace staff authorization metadata from an authenticated server
  /// response. The server returns the full small staff list, so old or
  /// deactivated rows cannot linger in this cache.
  Future<void> replaceCachedStaff(List<Map<String, dynamic>> rows) async {
    await transaction(() async {
      await customStatement('DELETE FROM staff_cache');
      for (final row in rows) {
        await customInsert(
          'INSERT INTO staff_cache '
          '(id, username, full_name, role, offline_approval_hash, '
          'offline_approval_version, is_active, updated_at) '
          'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          variables: [
            Variable<String>(row['id'] as String),
            Variable<String>(row['username'] as String),
            Variable<String>(row['full_name'] as String),
            Variable<String>(row['role'] as String),
            Variable<String>(row['offline_approval_hash'] as String? ?? ''),
            Variable<int>(row['offline_approval_version'] as int),
            Variable<int>((row['is_active'] as bool) ? 1 : 0),
            Variable<int>(
              DateTime.parse(
                row['updated_at'] as String,
              ).millisecondsSinceEpoch,
            ),
          ],
          updates: {staffCache},
        );
      }
    });
  }

  Future<CachedApprovalStaff?> cachedApprovalStaff(String username) async {
    final rows = await customSelect(
      'SELECT id, username, full_name, role, offline_approval_hash, '
      'offline_approval_version, is_active, updated_at '
      'FROM staff_cache WHERE username = ? COLLATE NOCASE LIMIT 1',
      variables: [Variable<String>(username.trim())],
    ).get();
    if (rows.isEmpty) return null;
    final row = rows.single.data;
    return CachedApprovalStaff(
      id: row['id'] as String,
      username: row['username'] as String,
      fullName: row['full_name'] as String,
      role: row['role'] as String,
      verifier: row['offline_approval_hash'] as String,
      version: row['offline_approval_version'] as int,
      isActive: (row['is_active'] as int) == 1,
      updatedAt: DateTime.fromMillisecondsSinceEpoch(row['updated_at'] as int),
    );
  }

  Future<List<Map<String, dynamic>>> cachedApprovalStaffRows() async {
    final rows = await customSelect(
      'SELECT id, username, full_name, role, offline_approval_hash, '
      'offline_approval_version, is_active, updated_at FROM staff_cache '
      'ORDER BY username COLLATE NOCASE',
    ).get();
    return [for (final row in rows) row.data];
  }

  @override
  MigrationStrategy get migration => MigrationStrategy(
    onCreate: (m) async => m.createAll(),
    onUpgrade: (m, from, to) async {
      if (from < 2) {
        await m.database.customStatement(
          'ALTER TABLE staff_cache RENAME TO staff_cache_legacy',
        );
        await m.createTable(staffCache);
        await m.database.customStatement(
          'INSERT INTO staff_cache '
          '(id, username, full_name, role, offline_approval_hash, '
          'offline_approval_version, is_active, updated_at) '
          'SELECT id, username, full_name, role, "", 0, is_active, updated_at '
          'FROM staff_cache_legacy',
        );
        await m.database.customStatement('DROP TABLE staff_cache_legacy');
      }
    },
  );

  @override
  int get schemaVersion => 2;
}

class CachedApprovalStaff {
  const CachedApprovalStaff({
    required this.id,
    required this.username,
    required this.fullName,
    required this.role,
    required this.verifier,
    required this.version,
    required this.isActive,
    required this.updatedAt,
  });

  final String id;
  final String username;
  final String fullName;
  final String role;
  final String verifier;
  final int version;
  final bool isActive;
  final DateTime updatedAt;
}
