/// Taking the money when the network may or may not be there.
///
/// The case worth most of this file: a request that hung and *succeeded
/// invisibly*. Queue that sale under a fresh identifier and the customer is
/// charged twice in the shop's books.
library;

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/api_client.dart';
import 'package:pos_till/data/cart/cart_controller.dart';
import 'package:pos_till/data/cart/checkout_service.dart';
import 'package:pos_till/data/cart/pricing.dart';
import 'package:pos_till/data/outbox/database.dart';
import 'package:pos_till/data/outbox/outbox_repository.dart';
import 'package:pos_till/data/outbox/pin_lockout.dart';

class FakeCheckout implements CheckoutTransport {
  final List<Map<String, dynamic>> sent = [];
  ApiException? throwThis;
  Map<String, dynamic> reply = const {'id': 'server-1', 'receipt_number': 7};

  @override
  Future<Map<String, dynamic>> postCashSale(Map<String, dynamic> body) async {
    sent.add(body);
    if (throwThis != null) throw throwThis!;
    return reply;
  }
}

class FakeSync implements SyncTransport {
  final List<Map<String, dynamic>> sent = [];
  Map<String, dynamic> Function(Map<String, dynamic>)? respond;

  @override
  Future<Map<String, dynamic>> postBatch(Map<String, dynamic> body) async {
    sent.add(body);
    return respond?.call(body) ?? {'results': const [], 'refusals_recorded': 0};
  }
}

const offline = ApiException(
  status: 0,
  code: 'offline',
  message: 'No connection to the server.',
);

CartState cartWith({
  int price = 18000,
  int discountBps = 0,
  CartApproval? approval,
}) {
  final controller = CartController()
    ..add(
      LineInput(
        itemId: 'sugar',
        name: 'Sugar 1kg',
        unitPriceCents: price,
        quantityMilli: 1000,
        taxRateBps: 1600,
      ),
    );
  if (discountBps > 0) {
    controller.discountCart(bps: discountBps, reason: 'Damaged packaging');
  }
  if (approval != null) controller.approve(approval);
  return controller.state;
}

void main() {
  late OutboxDatabase db;
  late FakeCheckout checkout;
  late FakeSync syncTransport;
  late OutboxRepository outbox;
  late CheckoutService service;

  setUp(() {
    db = OutboxDatabase(NativeDatabase.memory());
    checkout = FakeCheckout();
    syncTransport = FakeSync();
    outbox = OutboxRepository(
      database: db,
      transport: syncTransport,
      lockout: PinLockout(db),
    );
    service = CheckoutService(transport: checkout, outbox: outbox);
  });

  tearDown(() => db.close());

  group('with a connection', () {
    test('the sale settles and comes back with a receipt number', () async {
      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      expect(result.outcome, CheckoutOutcome.settled);
      expect(result.receiptNumber, 7);
      expect(result.reference, '#7');
      expect(await outbox.pendingCount(), 0);
    });

    test('change is worked out from the rounded figure', () async {
      // The customer is asked for the rounded amount, so change has to be
      // measured against that and not against the exact total.
      final result = await service.takeCash(
        cart: cartWith(price: 18049),
        tenderedCents: 20000,
      );

      expect(result.totals.cashDueCents, 18000);
      expect(result.changeCents, 2000);
    });

    test('the request carries the client uuid', () async {
      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      expect(checkout.sent.single['client_uuid'], result.clientUuid);
    });

    test('a fractional quantity travels as a decimal string', () async {
      final controller = CartController()
        ..add(
          const LineInput(
            itemId: 'sugar',
            name: 'Sugar',
            unitPriceCents: 18000,
            quantityMilli: 333,
            taxRateBps: 1600,
          ),
        );

      await service.takeCash(cart: controller.state, tenderedCents: 10000);

      final lines = checkout.sent.single['lines'] as List;
      expect((lines.single as Map)['quantity'], '0.333');
    });

    test("a manager's own approval sends no username", () async {
      // Their session is the authority. Sending a username would invite the
      // server to verify something nobody typed.
      await service.takeCash(
        cart: cartWith(
          discountBps: 1000,
          approval: CartApproval(
            username: 'grace',
            reason: 'Damaged packaging',
            method: 'SESSION',
            at: DateTime(2026, 8, 14),
          ),
        ),
        tenderedCents: 20000,
      );

      final approval =
          checkout.sent.single['discount_authorization'] as Map<String, dynamic>;
      expect(approval.containsKey('username'), isFalse);
      expect(approval['reason'], 'Damaged packaging');
    });

    test("a cashier's delegated approval names the manager", () async {
      await service.takeCash(
        cart: cartWith(
          discountBps: 1000,
          approval: CartApproval(
            username: 'grace',
            reason: 'Damaged packaging',
            method: 'PIN',
            at: DateTime(2026, 8, 14),
          ),
        ),
        tenderedCents: 20000,
      );

      final approval =
          checkout.sent.single['discount_authorization'] as Map<String, dynamic>;
      expect(approval['username'], 'grace');
    });
  });

  group('with no connection', () {
    test('the sale is queued rather than lost', () async {
      checkout.throwThis = offline;

      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      expect(result.outcome, CheckoutOutcome.queued);
      expect(await outbox.pendingCount(), 1);
    });

    test('the customer gets a reference that is not a receipt number', () async {
      // A cashier or customer who mistook one for the other would quote a
      // number belonging to a different sale entirely.
      checkout.throwThis = offline;

      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      expect(result.provisionalReference, startsWith('TMP-'));
      expect(result.reference, isNot(startsWith('#')));
      expect(result.receiptNumber, isNull);
    });

    test('a known-offline till does not wait out a timeout', () async {
      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
        forceOffline: true,
      );

      expect(checkout.sent, isEmpty);
      expect(result.outcome, CheckoutOutcome.queued);
    });

    test('an offline approval keeps the version it checked', () async {
      checkout.throwThis = offline;

      await service.takeCash(
        cart: cartWith(
          discountBps: 1000,
          approval: CartApproval(
            username: 'grace',
            reason: 'Damaged packaging',
            method: 'OFFLINE',
            at: DateTime(2026, 8, 14),
            pinVersion: 4,
          ),
        ),
        tenderedCents: 20000,
      );

      syncTransport.respond = (body) => {
            'results': [
              {
                'client_uuid':
                    (body['sales'] as List).single['client_uuid'],
                'status': SyncStatus.accepted,
                'sale_id': 's1',
                'receipt_number': 9,
                'flags': const [],
              },
            ],
            'refusals_recorded': 0,
          };
      await outbox.sync(deviceId: 'till-1');

      final approval = (syncTransport.sent.single['sales'] as List)
          .single['discount_authorization'] as Map;
      expect(approval['pin_version'], 4);
      expect(approval.containsKey('pin'), isFalse);
    });
  });

  group('a request that may have succeeded invisibly', () {
    test('the queued sale reuses the identifier that was already sent',
        () async {
      // The whole reason the uuid is generated before the attempt. A fresh one
      // here means the sync creates a second sale for money taken once.
      checkout.throwThis = offline;

      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      final attempted = checkout.sent.single['client_uuid'];
      final queued = (await outbox.pending()).single;
      expect(queued.clientUuid, attempted);
      expect(queued.clientUuid, result.clientUuid);
    });

    test('the server recognises it as a replay rather than a new sale',
        () async {
      checkout.throwThis = offline;
      await service.takeCash(cart: cartWith(), tenderedCents: 20000);

      syncTransport.respond = (body) => {
            'results': [
              {
                'client_uuid': (body['sales'] as List).single['client_uuid'],
                'status': SyncStatus.duplicate,
                'sale_id': 'the-original',
                'receipt_number': 7,
                'flags': const [],
              },
            ],
            'refusals_recorded': 0,
          };

      final report = await outbox.sync(deviceId: 'till-1');

      expect(report.duplicate, 1);
      expect(report.needsAttention, isFalse);
      expect(await outbox.pendingCount(), 0);
    });

    test('a server error is queued, not treated as a refusal', () async {
      // A 500 says nothing about whether the sale was written, and the cash is
      // in the drawer either way.
      checkout.throwThis = const ApiException(
        status: 500,
        code: 'error',
        message: 'Something went wrong.',
      );

      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      expect(result.outcome, CheckoutOutcome.queued);
    });
  });

  group('a refusal the server means', () {
    test('a discount with no authority is refused, not queued', () async {
      // Queueing it would reproduce the same refusal at sync, with the cashier
      // no longer at the counter to fix it.
      checkout.throwThis = const ApiException(
        status: 403,
        code: 'discount_authorization_required',
        message: 'A discount needs a manager.',
      );

      final result = await service.takeCash(
        cart: cartWith(discountBps: 1000),
        tenderedCents: 20000,
      );

      expect(result.outcome, CheckoutOutcome.refused);
      expect(result.error!.code, 'discount_authorization_required');
      expect(await outbox.pendingCount(), 0);
    });

    test('an unknown item is refused', () async {
      checkout.throwThis = const ApiException(
        status: 400,
        code: 'item_not_found',
        message: 'No such item.',
      );

      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 20000,
      );

      expect(result.isRefused, isTrue);
      expect(await outbox.pendingCount(), 0);
    });
  });

  group('tender', () {
    test('tendering less than the amount due is refused outright', () async {
      // A live sale is not finished when the cashier is holding too few notes,
      // and the customer is still standing there to make it up.
      expect(
        () => service.takeCash(cart: cartWith(), tenderedCents: 100),
        throwsA(isA<PricingError>()),
      );
    });

    test('exact tender leaves no change', () async {
      final result = await service.takeCash(
        cart: cartWith(),
        tenderedCents: 18000,
      );

      expect(result.changeCents, 0);
    });
  });
}
