import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/outbox/database.dart';
import 'package:sqlite3/sqlite3.dart' as sqlite;

void main() {
  late Directory directory;

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('pos-outbox-encryption-');
  });

  tearDown(() async {
    await directory.delete(recursive: true);
  });

  test('recovery keys must retain the full randomly generated key', () {
    final validPrefix = List.filled(64, 'a').join();
    final shortPrefix = List.filled(63, 'a').join();
    expect(OutboxDatabase.isValidRecoveryKey(validPrefix), isTrue);
    expect(OutboxDatabase.isValidRecoveryKey(shortPrefix), isFalse);
    expect(OutboxDatabase.isValidRecoveryKey('${shortPrefix}z'), isFalse);
    expect(
      OutboxDatabase.isValidRecoveryKey(List.filled(64, ' ').join()),
      isFalse,
    );
  });

  test(
    'encrypts and verifies the existing database before replacing it',
    () async {
      final source = File('${directory.path}/outbox.sqlite');
      final temp = File('${directory.path}/outbox.sqlite.encrypting');
      final backup = File('${directory.path}/outbox.sqlite.plaintext-backup');
      final plaintext = sqlite.sqlite3.open(source.path);
      try {
        plaintext.execute(
          'CREATE TABLE queued_sales ('
          'client_uuid TEXT PRIMARY KEY, payload_json TEXT, status TEXT)',
        );
        plaintext.execute(
          "INSERT INTO queued_sales VALUES "
          "('unsynced-sale-1', '{\"total\":1234}', 'pending')",
        );
      } finally {
        plaintext.close();
      }

      await OutboxDatabase.migratePlaintextDatabase(
        source,
        temp,
        backup,
        'a-test-key-with-sufficient-randomness',
      );

      expect(await source.exists(), isTrue);
      expect(await backup.exists(), isFalse);
      final header = await source.open();
      final bytes = await header.read(16);
      await header.close();
      expect(String.fromCharCodes(bytes), isNot('SQLite format 3\u0000'));

      final encrypted = sqlite.sqlite3.open(source.path);
      try {
        expect(encrypted.select('PRAGMA cipher;'), isNotEmpty);
        encrypted.execute(
          "PRAGMA key = 'a-test-key-with-sufficient-randomness';",
        );
        expect(
          encrypted
              .select('SELECT client_uuid FROM queued_sales')
              .single['client_uuid'],
          'unsynced-sale-1',
        );
      } finally {
        encrypted.close();
      }

      final wrongKey = sqlite.sqlite3.open(source.path);
      try {
        wrongKey.execute("PRAGMA key = 'wrong-key';");
        expect(
          () => wrongKey.select('SELECT count(*) FROM sqlite_master'),
          throwsA(isA<sqlite.SqliteException>()),
        );
      } finally {
        wrongKey.close();
      }
    },
  );

  test('leaves the plaintext outbox untouched when copying fails', () async {
    final source = File('${directory.path}/outbox.sqlite');
    final missingParent = Directory('${directory.path}/missing');
    final temp = File('${missingParent.path}/outbox.sqlite.encrypting');
    final backup = File('${directory.path}/outbox.sqlite.plaintext-backup');
    final plaintext = sqlite.sqlite3.open(source.path);
    try {
      plaintext.execute(
        'CREATE TABLE queued_sales ('
        'client_uuid TEXT PRIMARY KEY, payload_json TEXT, status TEXT)',
      );
      plaintext.execute(
        "INSERT INTO queued_sales VALUES "
        "('unsynced-sale-2', '{\"total\":4321}', 'pending')",
      );
    } finally {
      plaintext.close();
    }

    await expectLater(
      OutboxDatabase.migratePlaintextDatabase(
        source,
        temp,
        backup,
        'a-test-key-with-sufficient-randomness',
      ),
      throwsA(isA<sqlite.SqliteException>()),
    );

    expect(await source.exists(), isTrue);
    expect(await backup.exists(), isFalse);
    final stillPlaintext = sqlite.sqlite3.open(source.path);
    try {
      expect(
        stillPlaintext
            .select('SELECT client_uuid FROM queued_sales')
            .single['client_uuid'],
        'unsynced-sale-2',
      );
    } finally {
      stillPlaintext.close();
    }
  });
}
