/// Building a cart at a counter.
///
/// The cases here are the ones a cashier actually creates: scanning the same
/// thing twice, changing a quantity, discounting something and then editing it
/// afterwards.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/cart/cart_controller.dart';
import 'package:pos_till/data/cart/pricing.dart';

LineInput sugar({int quantityMilli = 1000, int discountBps = 0, int price = 18000}) =>
    LineInput(
      itemId: 'sugar',
      name: 'Sugar 1kg',
      unitPriceCents: price,
      quantityMilli: quantityMilli,
      taxRateBps: 1600,
      discountBps: discountBps,
    );

LineInput haircut({int price = 50000}) => LineInput(
      itemId: 'haircut',
      name: 'Braiding',
      unitPriceCents: price,
      quantityMilli: 1000,
    );

void main() {
  late CartController cart;

  setUp(() => cart = CartController());

  group('adding things', () {
    test('an empty cart totals nothing rather than throwing', () {
      expect(cart.state.isEmpty, isTrue);
      expect(cart.state.totals.totalCents, 0);
    });

    test('scanning the same item twice makes one line of two', () {
      // A receipt listing it twice makes a customer think they were charged
      // twice.
      cart.add(sugar());
      cart.add(sugar());

      expect(cart.state.lines, hasLength(1));
      expect(cart.state.lines.single.quantityMilli, 2000);
      expect(cart.state.totals.totalCents, 36000);
    });

    test('two different items stay two lines', () {
      cart.add(sugar());
      cart.add(haircut());

      expect(cart.state.lines, hasLength(2));
    });

    test('the same item at a different price stays two lines', () {
      // Two haircuts at different prices are genuinely two lines, and merging
      // them would discard one of the prices the cashier typed.
      cart.add(haircut(price: 50000), mergeable: false);
      cart.add(haircut(price: 80000), mergeable: false);

      expect(cart.state.lines, hasLength(2));
      expect(cart.state.totals.totalCents, 130000);
    });

    test('a discounted line does not merge with an undiscounted one', () {
      cart.add(sugar());
      cart.add(sugar(discountBps: 1000));

      expect(cart.state.lines, hasLength(2));
    });
  });

  group('changing a cart', () {
    test('a quantity can be corrected', () {
      cart.add(sugar());
      cart.setQuantity(0, 3000);

      expect(cart.state.totals.totalCents, 54000);
    });

    test('setting a quantity to zero removes the line', () {
      // A zero-quantity line on a receipt is a line the customer did not buy.
      cart.add(sugar());
      cart.setQuantity(0, 0);

      expect(cart.state.isEmpty, isTrue);
    });

    test('a line can be removed', () {
      cart.add(sugar());
      cart.add(haircut());
      cart.removeAt(0);

      expect(cart.state.lines.single.itemId, 'haircut');
    });

    test('an index that does not exist is ignored, not a crash', () {
      cart.add(sugar());
      cart.removeAt(9);
      cart.setQuantity(-1, 5000);

      expect(cart.state.lines, hasLength(1));
    });

    test('clearing empties everything, including a discount', () {
      cart.add(sugar());
      cart.discountCart(bps: 1000, reason: 'Damaged');
      cart.clear();

      expect(cart.state.isEmpty, isTrue);
      expect(cart.state.hasDiscount, isFalse);
      expect(cart.state.discountReason, isEmpty);
    });
  });

  group('totals', () {
    test('totals are derived, never stored', () {
      // A cached total is how a figure ends up disagreeing with the lines
      // printed above it.
      cart.add(sugar());
      final before = cart.state.totals.totalCents;
      cart.setQuantity(0, 2000);

      expect(before, 18000);
      expect(cart.state.totals.totalCents, 36000);
    });

    test('a cart discount reduces the total', () {
      cart.add(sugar());
      cart.discountCart(bps: 1000, reason: 'Regular customer');

      expect(cart.state.totals.totalCents, 16200);
      expect(cart.state.totals.discountCents, 1800);
    });

    test('the lines still sum to the total after a cart discount', () {
      cart.add(sugar());
      cart.add(haircut());
      cart.discountCart(cents: 1333, reason: 'Haggled');

      final totals = cart.state.totals;
      expect(
        totals.lines.fold<int>(0, (sum, line) => sum + line.grossCents),
        totals.totalCents,
      );
    });

    test('cash rounding is reported alongside the exact total', () {
      cart.add(sugar(price: 18049));

      expect(cart.state.totals.totalCents, 18049);
      expect(cart.state.totals.cashDueCents, 18000);
      expect(cart.state.totals.roundingAdjustmentCents, -49);
    });
  });

  group('a discount needs a name against it', () {
    test('a plain cart needs no authorisation', () {
      cart.add(sugar());

      expect(cart.state.hasDiscount, isFalse);
      expect(cart.state.needsAuthorization, isFalse);
    });

    test('a discounted cart is blocked until somebody approves it', () {
      cart.add(sugar());
      cart.discountCart(bps: 1000, reason: 'Damaged packaging');

      expect(cart.state.needsAuthorization, isTrue);
    });

    test('a line discount counts too', () {
      cart.add(sugar());
      cart.discountLine(0, bps: 500);

      expect(cart.state.hasDiscount, isTrue);
      expect(cart.state.needsAuthorization, isTrue);
    });

    test('approving it unblocks checkout', () {
      cart.add(sugar());
      cart.discountCart(bps: 1000, reason: 'Damaged packaging');
      cart.approve(
        CartApproval(
          username: 'grace',
          reason: 'Damaged packaging',
          method: 'PIN',
          at: DateTime(2026, 8, 14),
        ),
      );

      expect(cart.state.needsAuthorization, isFalse);
      expect(cart.state.approvedBy!.username, 'grace');
    });

    test('editing the discount afterwards revokes the approval', () {
      // The manager approved the discount they were shown, not whatever it was
      // afterwards edited into.
      cart.add(sugar());
      cart.discountCart(bps: 1000, reason: 'Damaged packaging');
      cart.approve(
        CartApproval(
          username: 'grace',
          reason: 'Damaged packaging',
          method: 'PIN',
          at: DateTime(2026, 8, 14),
        ),
      );

      cart.discountCart(bps: 5000, reason: 'Damaged packaging');

      expect(cart.state.approvedBy, isNull);
      expect(cart.state.needsAuthorization, isTrue);
    });

    test('editing a line discount also revokes it', () {
      cart.add(sugar());
      cart.discountLine(0, bps: 500);
      cart.approve(
        CartApproval(
          username: 'grace',
          reason: 'Damaged',
          method: 'PIN',
          at: DateTime(2026, 8, 14),
        ),
      );

      cart.discountLine(0, bps: 9000);

      expect(cart.state.approvedBy, isNull);
    });

    test('an offline approval carries the version it checked', () {
      cart.add(sugar());
      cart.discountCart(bps: 1000, reason: 'Damaged');
      cart.approve(
        CartApproval(
          username: 'grace',
          reason: 'Damaged',
          method: 'OFFLINE',
          at: DateTime(2026, 8, 14),
          pinVersion: 4,
        ),
      );

      expect(cart.state.approvedBy!.isOffline, isTrue);
      expect(cart.state.approvedBy!.pinVersion, 4);
    });
  });
}
