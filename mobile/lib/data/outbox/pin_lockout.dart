/// Rate-limiting PIN attempts on a till with no connection.
///
/// The server counts failed attempts in Redis. Offline, Redis is precisely the
/// thing that cannot be reached - so without this, a till with no signal would
/// accept unlimited guesses at a manager's four digits, in the one situation
/// where nobody is watching. Ten thousand combinations is an evening's work.
///
/// Three properties matter, and each is a way this goes wrong if it is written
/// casually:
///
/// **It is not cleared by reconnecting.** A lockout that could be lifted by
/// toggling aeroplane mode would not be a lockout. The count lives in the
/// outbox database and survives restarts, network changes and app kills.
///
/// **An unknown username is counted too.** Otherwise the counter itself would
/// answer the question "does this name exist?" - somebody could enumerate the
/// staff list by watching which names lock out and which never do.
///
/// **Every refusal is queued to be sent home.** A lockout that only the tablet
/// knows about tells the shop owner nothing. The attempts sync as
/// `DISCOUNT_REFUSED` entries on the next connection, which is what makes the
/// audit trail contain the failures and not only the successes.
library;

import 'dart:convert';

import 'package:drift/drift.dart';

import 'database.dart';

/// How many wrong PINs before the till stops asking.
///
/// Matches the server's threshold. Two different numbers would mean a manager
/// who mistypes twice is locked out at the counter but not in the back office,
/// and the shop would learn to distrust whichever one refused them.
const int kMaxOfflinePinFailures = 5;

/// How long the till stays shut after that.
///
/// Long enough that guessing is not worth the wait, short enough that a
/// manager who genuinely fat-fingered it can serve the next customer.
const Duration kOfflineLockoutWindow = Duration(minutes: 15);

/// Why an offline authorisation was refused. These strings cross the wire and
/// are the `reason_code` the server files the audit entry under, so they match
/// the server's vocabulary exactly rather than being invented here.
class OfflineRefusalReason {
  static const noAuthorization = 'no_authorization';
  static const unknownUser = 'unknown_user';
  static const badCredential = 'bad_credential';
  static const insufficientRole = 'insufficient_role';
  static const lockedOut = 'locked_out';
}

/// The result of asking whether a PIN may even be tried.
class LockoutState {
  const LockoutState({required this.isLocked, required this.remaining});

  final bool isLocked;
  final Duration remaining;

  static const open = LockoutState(isLocked: false, remaining: Duration.zero);
}

class PinLockout {
  PinLockout(this._db);

  final OutboxDatabase _db;

  /// Whether this name may be tried right now.
  ///
  /// Takes the username rather than a user id so that a name which matches
  /// nobody is rate-limited on the same footing as one that does.
  Future<LockoutState> check(String username, {DateTime? now}) async {
    final at = now ?? DateTime.now();
    final row = await _row(username);
    if (row == null || row.lockedUntil == null) return LockoutState.open;

    final until = DateTime.fromMillisecondsSinceEpoch(row.lockedUntil!);
    if (!until.isAfter(at)) return LockoutState.open;

    return LockoutState(isLocked: true, remaining: until.difference(at));
  }

  /// Record a wrong PIN, and lock the till if that was the last one allowed.
  ///
  /// Returns the state *after* the failure, so a caller can tell the person at
  /// the counter what just happened rather than making a second query for it.
  Future<LockoutState> recordFailure(
    String username, {
    String reason = OfflineRefusalReason.badCredential,
    DateTime? now,
  }) async {
    final at = now ?? DateTime.now();
    final row = await _row(username);
    final failures = (row?.failures ?? 0) + 1;

    final telemetry = _appendTelemetry(
      row?.pendingTelemetryJson ?? '[]',
      username: username,
      reason: reason,
      at: at,
    );

    final locked = failures >= kMaxOfflinePinFailures;
    final lockedUntil = locked ? at.add(kOfflineLockoutWindow) : null;

    await _db
        .into(_db.pinAttempts)
        .insertOnConflictUpdate(
          PinAttemptsCompanion.insert(
            scopeKey: username,
            failures: Value(failures),
            lockedUntil: Value(lockedUntil?.millisecondsSinceEpoch),
            lastFailureAt: Value(at),
            pendingTelemetryJson: Value(telemetry),
          ),
        );

    return locked
        ? LockoutState(isLocked: true, remaining: kOfflineLockoutWindow)
        : LockoutState.open;
  }

  /// Record that the till refused to even ask, because it was locked.
  ///
  /// Kept separate from [recordFailure] so a locked-out till's repeated
  /// attempts do not extend their own lockout indefinitely - which would let a
  /// bystander tapping at the screen keep a manager shut out - while still
  /// leaving every attempt in the trail that syncs home.
  Future<void> recordLockedOutAttempt(String username, {DateTime? now}) async {
    final at = now ?? DateTime.now();
    final row = await _row(username);
    if (row == null) return;

    await (_db.update(_db.pinAttempts)
          ..where((t) => t.scopeKey.equals(username)))
        .write(
          PinAttemptsCompanion(
            pendingTelemetryJson: Value(
              _appendTelemetry(
                row.pendingTelemetryJson,
                username: username,
                reason: OfflineRefusalReason.lockedOut,
                at: at,
              ),
            ),
          ),
        );
  }

  /// Forget the failures for a name that has just authorised successfully.
  ///
  /// The queued telemetry is deliberately *not* cleared. The successful
  /// attempt does not un-happen the four wrong ones before it, and those are
  /// exactly what a shop owner would want to see.
  Future<void> clearFailures(String username) async {
    final row = await _row(username);
    if (row == null) return;

    await (_db.update(_db.pinAttempts)
          ..where((t) => t.scopeKey.equals(username)))
        .write(
          const PinAttemptsCompanion(
            failures: Value(0),
            lockedUntil: Value(null),
          ),
        );
  }

  /// Every refusal waiting to be sent home, in the shape the sync endpoint
  /// takes.
  Future<List<Map<String, dynamic>>> pendingTelemetry() async {
    final rows = await _db.select(_db.pinAttempts).get();
    return [
      for (final row in rows)
        ...(jsonDecode(row.pendingTelemetryJson) as List)
            .cast<Map<String, dynamic>>(),
    ];
  }

  /// Drop the telemetry the server has now acknowledged.
  ///
  /// Called only after a successful sync. Clearing it optimistically before the
  /// response arrives would lose the entries on exactly the flaky connection
  /// this whole subsystem exists for.
  Future<void> clearTelemetry() async {
    await _db
        .update(_db.pinAttempts)
        .write(const PinAttemptsCompanion(pendingTelemetryJson: Value('[]')));
  }

  Future<PinAttempt?> _row(String username) =>
      (_db.select(_db.pinAttempts)
            ..where((t) => t.scopeKey.equals(username)))
          .getSingleOrNull();

  String _appendTelemetry(
    String existing, {
    required String username,
    required String reason,
    required DateTime at,
  }) {
    final entries = (jsonDecode(existing) as List).cast<dynamic>();
    entries.add({
      'username': username,
      'reason_code': reason,
      'occurred_at': at.toUtc().toIso8601String(),
    });
    return jsonEncode(entries);
  }
}
