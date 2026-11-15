import 'package:crypto/crypto.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/outbox/database.dart';
import 'package:pos_till/data/outbox/offline_approval_service.dart';
import 'package:pos_till/data/outbox/pin_lockout.dart';

void main() {
  late OutboxDatabase database;
  late PinLockout lockout;
  late OfflineApprovalService service;

  setUp(() async {
    database = OutboxDatabase(NativeDatabase.memory());
    lockout = PinLockout(database);
    service = OfflineApprovalService(database: database, lockout: lockout);
    await database.replaceCachedStaff([
      {
        'id': 'manager-id',
        'username': 'manager',
        'full_name': 'Shop Manager',
        'role': 'MANAGER',
        'offline_approval_hash': sha256
            .convert('separate-code'.codeUnits)
            .toString(),
        'offline_approval_version': 4,
        'is_active': true,
        'updated_at': '2026-11-13T12:00:00Z',
      },
      {
        'id': 'cashier-id',
        'username': 'cashier',
        'full_name': 'Shop Cashier',
        'role': 'CASHIER',
        'offline_approval_hash': '',
        'offline_approval_version': 0,
        'is_active': true,
        'updated_at': '2026-11-13T12:00:00Z',
      },
      {
        'id': 'inactive-id',
        'username': 'inactive',
        'full_name': 'Former Manager',
        'role': 'MANAGER',
        'offline_approval_hash': sha256
            .convert('separate-code'.codeUnits)
            .toString(),
        'offline_approval_version': 2,
        'is_active': false,
        'updated_at': '2026-11-13T12:00:00Z',
      },
    ]);
  });

  tearDown(() => database.close());

  test(
    'accepts the separate code for an active manager and returns its version',
    () async {
      final result = await service.approve(
        username: ' manager ',
        code: 'separate-code',
      );

      expect(result.accepted, isTrue);
      expect(result.staff?.username, 'manager');
      expect(result.staff?.version, 4);
    },
  );

  test('rejects a short sign-in PIN as an offline approval code', () async {
    final result = await service.approve(username: 'manager', code: '1234');

    expect(result.accepted, isFalse);
    expect(result.reason, OfflineRefusalReason.badCredential);
  });

  test(
    'does not allow a cashier or inactive manager to authorize offline',
    () async {
      final cashier = await service.approve(
        username: 'cashier',
        code: 'anything',
      );
      final inactive = await service.approve(
        username: 'inactive',
        code: 'separate-code',
      );

      expect(cashier.reason, OfflineRefusalReason.insufficientRole);
      expect(inactive.reason, OfflineRefusalReason.insufficientRole);
    },
  );

  test(
    'persists failures and locks the username after the configured threshold',
    () async {
      for (var attempt = 0; attempt < kMaxOfflinePinFailures; attempt++) {
        await service.approve(username: 'manager', code: 'wrong');
      }

      final locked = await service.approve(
        username: 'manager',
        code: 'separate-code',
      );
      expect(locked.reason, OfflineRefusalReason.lockedOut);
      expect(locked.lockedFor, isNotNull);
    },
  );
}
