import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/outbox/recovery_files.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('ke.pos.pos_till/recovery_files');

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  test(
    'export sends the private database path and suggested business name',
    () async {
      MethodCall? received;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, (call) async {
            received = call;
            return true;
          });

      final saved = await RecoveryFiles.exportEncryptedBackup(
        sourcePath: '/private/till.sqlite3',
        suggestedName: 'pos-till-shop-recovery-20261114.sqlite3',
      );

      expect(saved, isTrue);
      expect(received?.method, 'exportEncryptedBackup');
      expect(received?.arguments, {
        'sourcePath': '/private/till.sqlite3',
        'suggestedName': 'pos-till-shop-recovery-20261114.sqlite3',
      });
    },
  );

  test('import returns the private cache copy path', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
          channel,
          (call) async => '/private/cache/pos-till-recovery-import.sqlite3',
        );

    expect(
      await RecoveryFiles.importEncryptedBackup(),
      '/private/cache/pos-till-recovery-import.sqlite3',
    );
  });

  test('cancelled import returns null', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async => null);

    expect(await RecoveryFiles.importEncryptedBackup(), isNull);
  });
}
