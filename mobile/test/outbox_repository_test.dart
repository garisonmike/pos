/// What the till may delete, and what it must keep.
///
/// Every test here is a way money disappears if the outbox is written
/// optimistically: a sale cleared because the request looked like it worked, an
/// identifier regenerated on retry, a refusal dropped before the server took
/// it.
library;

import 'dart:convert';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/api_client.dart';
import 'package:pos_till/data/cart/pricing.dart';
import 'package:pos_till/data/outbox/database.dart';
import 'package:pos_till/data/outbox/outbox_repository.dart';
import 'package:pos_till/data/outbox/pin_lockout.dart';

/// A transport that answers whatever the test tells it to.
class FakeTransport implements SyncTransport {
  FakeTransport();

  final List<Map<String, dynamic>> sent = [];
  Map<String, dynamic> Function(Map<String, dynamic> body)? respond;
  ApiException? throwThis;

  @override
  Future<Map<String, dynamic>> postBatch(Map<String, dynamic> body) async {
    sent.add(body);
    if (throwThis != null) throw throwThis!;
    return respond?.call(body) ?? {'results': const [], 'refusals_recorded': 0};
  }
}

/// Every queued sale gets the verdict the test names, in order.
Map<String, dynamic> Function(Map<String, dynamic>) verdicts(
  List<String> statuses, {
  String? code,
  String? detail,
}) {
  return (body) {
    final sales = body['sales'] as List<dynamic>;
    return {
      'results': [
        for (var i = 0; i < sales.length; i++)
          {
            'client_uuid': (sales[i] as Map)['client_uuid'],
            'status': statuses[i % statuses.length],
            'sale_id': 'server-sale-$i',
            'receipt_number': 100 + i,
            'code': code,
            'detail': detail,
            'flags': const [],
          },
      ],
      'refusals_recorded': (body['refused_authorizations'] as List).length,
    };
  };
}

CartTotals oneItemCart({int priceCents = 18000, int quantityMilli = 1000}) {
  return priceCart([
    LineInput(
      itemId: 'item-1',
      name: 'Sugar 1kg',
      unitPriceCents: priceCents,
      quantityMilli: quantityMilli,
      taxRateBps: 1600,
    ),
  ]);
}

void main() {
  late OutboxDatabase db;
  late FakeTransport transport;
  late PinLockout lockout;
  late OutboxRepository outbox;

  setUp(() {
    db = OutboxDatabase(NativeDatabase.memory());
    transport = FakeTransport();
    lockout = PinLockout(db);
    outbox = OutboxRepository(
      database: db,
      transport: transport,
      lockout: lockout,
    );
  });

  tearDown(() => db.close());

  group('queueing a sale', () {
    test('a completed sale lands in the outbox', () async {
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);

      expect(await outbox.pendingCount(), 1);
    });

    test('the payload carries what the sync endpoint needs', () async {
      final queued = await outbox.enqueue(
        totals: oneItemCart(),
        tenderedCents: 18000,
      );
      final payload = jsonDecode(queued.payloadJson) as Map<String, dynamic>;

      expect(payload['client_uuid'], queued.clientUuid);
      expect(payload['tendered_cents'], 18000);
      expect(payload['total_cents'], 18000);
      expect(payload['device_created_at'], endsWith('Z'));
      expect((payload['lines'] as List).single['item_id'], 'item-1');
    });

    test('a fractional quantity travels as a decimal string', () async {
      // A double would occasionally arrive as 0.3330000000000001, and the
      // server's quantity field has three decimal places.
      final queued = await outbox.enqueue(
        totals: oneItemCart(quantityMilli: 333),
        tenderedCents: 6000,
      );
      final payload = jsonDecode(queued.payloadJson) as Map<String, dynamic>;

      expect((payload['lines'] as List).single['quantity'], '0.333');
    });

    test('each sale takes the next number in the device series', () async {
      final first = await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      final second = await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);

      expect(second.deviceSequence, first.deviceSequence + 1);
    });

    test('the series does not repeat itself after rows are cleared', () async {
      // Derived from a watermark rather than a row count, so that deleting
      // settled sales cannot make a later sale reuse an earlier number - a
      // reused number would hide a gap instead of showing one.
      final first = await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts([SyncStatus.accepted]);
      await outbox.sync(deviceId: 'till-1');
      expect(await outbox.pendingCount(), 0);

      final next = await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      expect(next.deviceSequence, greaterThan(first.deviceSequence));
    });

    test('an offline approval travels without any credential', () async {
      final queued = await outbox.enqueue(
        totals: oneItemCart(),
        tenderedCents: 18000,
        approval: OfflineApproval(
          username: 'grace',
          reason: 'Damaged packaging',
          pinVersion: 3,
          authorizedAt: DateTime.utc(2026, 8, 14, 9),
        ),
      );
      final payload = jsonDecode(queued.payloadJson) as Map<String, dynamic>;
      final approval = payload['discount_authorization'] as Map<String, dynamic>;

      expect(approval['username'], 'grace');
      expect(approval['pin_version'], 3);
      // A replayed batch must never be a bundle of manager PINs in flight.
      expect(approval.containsKey('pin'), isFalse);
      expect(approval.containsKey('password'), isFalse);
    });
  });

  group('applying the server verdicts', () {
    test('an accepted sale leaves the outbox', () async {
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts([SyncStatus.accepted]);

      final report = await outbox.sync(deviceId: 'till-1');

      expect(report.accepted, 1);
      expect(await outbox.pendingCount(), 0);
    });

    test('a duplicate also leaves the outbox', () async {
      // A replay is a success. Treating it as a failure would leave rows here
      // forever, and the till would never stop resending them.
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts([SyncStatus.duplicate]);

      final report = await outbox.sync(deviceId: 'till-1');

      expect(report.duplicate, 1);
      expect(report.needsAttention, isFalse);
      expect(await outbox.pendingCount(), 0);
    });

    test('a rejected sale is kept, with the reason the server gave', () async {
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts(
        [SyncStatus.rejected],
        code: 'item_unavailable',
        detail: 'That item is no longer sold.',
      );

      final report = await outbox.sync(deviceId: 'till-1');
      final stuck = await outbox.rejected();

      expect(report.rejected, 1);
      expect(report.needsAttention, isTrue);
      expect(stuck.single.failureCode, 'item_unavailable');
      expect(stuck.single.failureDetail, 'That item is no longer sold.');
    });

    test('a rejected sale is not resent on the next sync', () async {
      // Retrying it unchanged cannot help, and hiding it in the pending queue
      // would stop anybody ever looking at it.
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts([SyncStatus.rejected]);
      await outbox.sync(deviceId: 'till-1');

      await outbox.sync(deviceId: 'till-1');

      expect(transport.sent.length, 1);
    });

    test('one rejection does not strand the rest of the batch', () async {
      for (var i = 0; i < 3; i++) {
        await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      }
      transport.respond = verdicts([
        SyncStatus.accepted,
        SyncStatus.rejected,
        SyncStatus.accepted,
      ]);

      final report = await outbox.sync(deviceId: 'till-1');

      expect(report.accepted, 2);
      expect(report.rejected, 1);
      expect(await outbox.pendingCount(), 0);
      expect((await outbox.rejected()).length, 1);
    });
  });

  group('when the request never arrives', () {
    test('nothing is deleted', () async {
      // The request may have hung for ninety seconds and succeeded invisibly.
      // "I sent it" is not evidence that it arrived.
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.throwThis = const ApiException(
        status: 0,
        code: 'offline',
        message: 'No connection.',
      );

      final report = await outbox.sync(deviceId: 'till-1');

      expect(report.reachedServer, isFalse);
      expect(report.failure!.isOffline, isTrue);
      expect(await outbox.pendingCount(), 1);
    });

    test('the same identifier is sent again, never a new one', () async {
      // Regenerating it is precisely how one sale becomes two.
      final queued = await outbox.enqueue(
        totals: oneItemCart(),
        tenderedCents: 18000,
      );
      transport.throwThis = const ApiException(
        status: 0,
        code: 'offline',
        message: 'No connection.',
      );
      await outbox.sync(deviceId: 'till-1');

      transport.throwThis = null;
      transport.respond = verdicts([SyncStatus.duplicate]);
      await outbox.sync(deviceId: 'till-1');

      final firstId = (transport.sent[0]['sales'] as List).single['client_uuid'];
      final secondId = (transport.sent[1]['sales'] as List).single['client_uuid'];
      expect(secondId, firstId);
      expect(firstId, queued.clientUuid);
    });

    test('the attempt is still counted, so a stuck sale is visible', () async {
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.throwThis = const ApiException(
        status: 0,
        code: 'offline',
        message: 'No connection.',
      );

      await outbox.sync(deviceId: 'till-1');
      await outbox.sync(deviceId: 'till-1');

      expect((await outbox.pending()).single.attempts, 2);
    });
  });

  group('sending refusals home', () {
    test('offline refusals ride along with the batch', () async {
      await lockout.recordFailure('grace');
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts([SyncStatus.accepted]);

      final report = await outbox.sync(deviceId: 'till-1');

      expect((transport.sent.single['refused_authorizations'] as List), hasLength(1));
      expect(report.refusalsSent, 1);
      expect(await lockout.pendingTelemetry(), isEmpty);
    });

    test('refusals sync even with no sales queued', () async {
      await lockout.recordFailure('grace');
      transport.respond = verdicts(const []);

      await outbox.sync(deviceId: 'till-1');

      expect(transport.sent, hasLength(1));
      expect((transport.sent.single['sales'] as List), isEmpty);
    });

    test('refusals are kept when the request fails', () async {
      await lockout.recordFailure('grace');
      transport.throwThis = const ApiException(
        status: 0,
        code: 'offline',
        message: 'No connection.',
      );

      await outbox.sync(deviceId: 'till-1');

      expect(await lockout.pendingTelemetry(), hasLength(1));
    });

    test('nothing is sent when there is nothing to send', () async {
      final report = await outbox.sync(deviceId: 'till-1');

      expect(transport.sent, isEmpty);
      expect(report.settled, 0);
    });
  });

  group('batch size', () {
    test('a large backlog goes up in capped batches', () async {
      for (var i = 0; i < kMaxSalesPerBatch + 5; i++) {
        await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      }
      transport.respond = verdicts([SyncStatus.accepted]);

      await outbox.sync(deviceId: 'till-1');

      expect((transport.sent.single['sales'] as List), hasLength(kMaxSalesPerBatch));
      expect(await outbox.pendingCount(), 5);
    });

    test('the rest goes on the next sync', () async {
      for (var i = 0; i < kMaxSalesPerBatch + 5; i++) {
        await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      }
      transport.respond = verdicts([SyncStatus.accepted]);

      await outbox.sync(deviceId: 'till-1');
      await outbox.sync(deviceId: 'till-1');

      expect(await outbox.pendingCount(), 0);
    });

    test('the oldest sales go first', () async {
      final first = await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      await outbox.enqueue(totals: oneItemCart(), tenderedCents: 18000);
      transport.respond = verdicts([SyncStatus.accepted]);

      await outbox.sync(deviceId: 'till-1');

      final sent = transport.sent.single['sales'] as List;
      expect((sent.first as Map)['client_uuid'], first.clientUuid);
    });
  });
}
