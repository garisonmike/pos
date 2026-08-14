/// The offline lockout is the counter that has to work when Redis does not.
///
/// Each test here is a way somebody gets in: guessing all evening, guessing at
/// a name to find out whether it exists, or clearing the count by turning the
/// network off and on.
library;

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/outbox/database.dart';
import 'package:pos_till/data/outbox/pin_lockout.dart';

void main() {
  late OutboxDatabase db;
  late PinLockout lockout;

  setUp(() {
    db = OutboxDatabase(NativeDatabase.memory());
    lockout = PinLockout(db);
  });

  tearDown(() => db.close());

  group('stopping a guess', () {
    test('a fresh name may be tried', () async {
      final state = await lockout.check('grace');
      expect(state.isLocked, isFalse);
    });

    test('four wrong PINs do not lock the till', () async {
      for (var i = 0; i < 4; i++) {
        await lockout.recordFailure('grace');
      }
      expect((await lockout.check('grace')).isLocked, isFalse);
    });

    test('the fifth wrong PIN locks it', () async {
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace');
      }
      expect((await lockout.check('grace')).isLocked, isTrue);
    });

    test('recordFailure reports the lockout it just caused', () async {
      LockoutState? last;
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        last = await lockout.recordFailure('grace');
      }
      expect(last!.isLocked, isTrue);
      expect(last.remaining, kOfflineLockoutWindow);
    });

    test('the lockout lifts once its window has passed', () async {
      final start = DateTime(2026, 8, 14, 9);
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace', now: start);
      }

      final duringWindow = await lockout.check(
        'grace',
        now: start.add(const Duration(minutes: 14)),
      );
      final afterWindow = await lockout.check(
        'grace',
        now: start.add(kOfflineLockoutWindow).add(const Duration(seconds: 1)),
      );

      expect(duringWindow.isLocked, isTrue);
      expect(afterWindow.isLocked, isFalse);
    });

    test('locking one name does not lock another', () async {
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace');
      }
      expect((await lockout.check('joseph')).isLocked, isFalse);
    });

    test('a name that matches nobody is counted just the same', () async {
      // Otherwise the counter answers "does this name exist?", and the staff
      // list can be enumerated by watching which names lock out.
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure(
          'nobody',
          reason: OfflineRefusalReason.unknownUser,
        );
      }
      expect((await lockout.check('nobody')).isLocked, isTrue);
    });

    test('a successful authorisation forgets the failures', () async {
      await lockout.recordFailure('grace');
      await lockout.recordFailure('grace');
      await lockout.clearFailures('grace');

      for (var i = 0; i < 4; i++) {
        await lockout.recordFailure('grace');
      }
      expect((await lockout.check('grace')).isLocked, isFalse);
    });

    test('attempts made while locked out do not extend the lockout', () async {
      // Otherwise a bystander tapping at the screen could keep a manager shut
      // out indefinitely.
      final start = DateTime(2026, 8, 14, 9);
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace', now: start);
      }
      await lockout.recordLockedOutAttempt(
        'grace',
        now: start.add(const Duration(minutes: 10)),
      );

      final after = await lockout.check(
        'grace',
        now: start.add(kOfflineLockoutWindow).add(const Duration(seconds: 1)),
      );
      expect(after.isLocked, isFalse);
    });
  });

  group('sending the refusals home', () {
    test('every failure is queued for the next sync', () async {
      await lockout.recordFailure('grace');
      await lockout.recordFailure('grace');

      final pending = await lockout.pendingTelemetry();
      expect(pending, hasLength(2));
      expect(pending.first['username'], 'grace');
      expect(pending.first['reason_code'], OfflineRefusalReason.badCredential);
    });

    test('an attempt made while locked out is queued under its own reason',
        () async {
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace');
      }
      await lockout.recordLockedOutAttempt('grace');

      final reasons = (await lockout.pendingTelemetry())
          .map((entry) => entry['reason_code'])
          .toList();
      expect(reasons.last, OfflineRefusalReason.lockedOut);
    });

    test('refusals for different names all come back', () async {
      await lockout.recordFailure('grace');
      await lockout.recordFailure('joseph');

      final names = (await lockout.pendingTelemetry())
          .map((entry) => entry['username'])
          .toSet();
      expect(names, {'grace', 'joseph'});
    });

    test('a success does not erase the failures that preceded it', () async {
      // The successful attempt does not un-happen the wrong ones, and those
      // are exactly what a shop owner would want to see.
      await lockout.recordFailure('grace');
      await lockout.recordFailure('grace');
      await lockout.clearFailures('grace');

      expect(await lockout.pendingTelemetry(), hasLength(2));
    });

    test('telemetry is only dropped once the server has taken it', () async {
      await lockout.recordFailure('grace');
      expect(await lockout.pendingTelemetry(), hasLength(1));

      await lockout.clearTelemetry();
      expect(await lockout.pendingTelemetry(), isEmpty);
    });

    test('clearing telemetry does not unlock the till', () async {
      // Syncing is not a reason to forgive five wrong PINs.
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace');
      }
      await lockout.clearTelemetry();

      expect((await lockout.check('grace')).isLocked, isTrue);
    });

    test('a refusal carries when it happened, in UTC', () async {
      await lockout.recordFailure('grace', now: DateTime.utc(2026, 8, 14, 9, 30));

      final entry = (await lockout.pendingTelemetry()).single;
      expect(entry['occurred_at'], startsWith('2026-08-14T09:30'));
      expect(entry['occurred_at'], endsWith('Z'));
    });
  });

  group('surviving a restart', () {
    test('the count is in the database, not in memory', () async {
      for (var i = 0; i < kMaxOfflinePinFailures; i++) {
        await lockout.recordFailure('grace');
      }

      // A second instance over the same database is what an app restart looks
      // like. A lockout held only in memory would evaporate here.
      final afterRestart = PinLockout(db);
      expect((await afterRestart.check('grace')).isLocked, isTrue);
    });
  });
}
