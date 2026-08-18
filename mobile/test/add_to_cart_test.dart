/// The catalogue-to-cart connection.
///
/// Written after finding, on a real handset, that it did not exist. The cart,
/// the tender screen and the receipt were all built and tested in milestone 3,
/// and nothing in the app could reach any of them: the item sheet still carried
/// a disabled "Selling comes next" placeholder from milestone 2, and no widget
/// anywhere navigated to CartScreen.
///
/// No test caught it because every test exercised one half. The cart controller
/// was proved in isolation, the cart screen was proved with a pre-filled cart,
/// and nothing asked whether a person holding the phone could get from one to
/// the other. These tests ask exactly that.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/theme.dart';
import 'package:pos_till/data/cart/cart_controller.dart';
import 'package:pos_till/data/models.dart';
import 'package:pos_till/features/catalog/item_sheet.dart';

/// A container so the test can read the cart the sheet wrote into.
late ProviderContainer container;

Widget wrap(Widget child) => UncontrolledProviderScope(
  container: container,
  child: MaterialApp(theme: TillTheme.build(), home: child),
);

Widget sheetOpener(Item item) => Builder(
  builder: (context) => Scaffold(
    body: TextButton(
      onPressed: () => showItemSheet(context, item),
      child: const Text('open'),
    ),
  ),
);

Item itemFrom(Map<String, Object?> overrides) => Item.fromJson({
  'id': 'item-1',
  'sku': 'SUGAR-1KG',
  'name': 'Sugar 1kg',
  'price_cents': 18000,
  'track_stock': true,
  'stock': const [
    {'store_code': 'MAIN', 'quantity': '40.000', 'is_low': false},
  ],
  ...overrides,
});

void main() {
  setUp(() => container = ProviderContainer());
  tearDown(() => container.dispose());

  CartState cart() => container.read(cartControllerProvider);

  group('Adding from the item sheet', () {
    testWidgets('a plain item reaches the cart', (tester) async {
      await tester.pumpWidget(wrap(sheetOpener(itemFrom(const {}))));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      expect(
        find.text('Add to cart'),
        findsOneWidget,
        reason: 'the sheet still shows the milestone 2 placeholder',
      );

      await tester.tap(find.text('Add to cart'));
      await tester.pumpAndSettle();

      expect(cart().lines, hasLength(1));
      expect(cart().lines.single.name, 'Sugar 1kg');
      expect(cart().lines.single.unitPriceCents, 18000);
      // One unit, expressed in thousandths.
      expect(cart().lines.single.quantityMilli, 1000);
    });

    testWidgets('an unavailable item cannot be added', (tester) async {
      final item = itemFrom(const {'is_available': false});
      await tester.pumpWidget(wrap(sheetOpener(item)));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      expect(find.text('Not available'), findsOneWidget);
      expect(find.text('Add to cart'), findsNothing);

      // Refused at the sheet rather than at the till, so a cashier finds out
      // while looking at the item and not with a customer waiting.
      final button = tester.widget<OutlinedButton>(find.byType(OutlinedButton));
      expect(button.onPressed, isNull);
      expect(cart().lines, isEmpty);
    });

    testWidgets('the same item twice becomes one line of two', (tester) async {
      await tester.pumpWidget(wrap(sheetOpener(itemFrom(const {}))));

      for (var i = 0; i < 2; i++) {
        await tester.tap(find.text('open'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('Add to cart'));
        await tester.pumpAndSettle();
      }

      expect(cart().lines, hasLength(1), reason: 'identical lines should merge');
      expect(cart().lines.single.quantityMilli, 2000);
    });
  });

  group('A price typed at the till', () {
    testWidgets('is asked for before the item is added', (tester) async {
      final item = itemFrom(const {
        'sku': 'SVC-BRAID',
        'name': 'Braiding',
        'is_price_variable': true,
        'track_stock': false,
        'stock': <Object?>[],
      });

      await tester.pumpWidget(wrap(sheetOpener(item)));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Add to cart'));
      await tester.pumpAndSettle();

      // Nothing is in the cart yet: asking first, because adding at zero and
      // correcting afterwards is how a sale goes out at zero.
      expect(cart().lines, isEmpty);
      expect(find.text('Price for Braiding'), findsOneWidget);

      await tester.enterText(find.byType(TextField), '450');
      await tester.tap(find.text('Add'));
      await tester.pumpAndSettle();

      expect(cart().lines, hasLength(1));
      expect(cart().lines.single.unitPriceCents, 45000);
    });

    testWidgets('cancelling adds nothing', (tester) async {
      final item = itemFrom(const {
        'name': 'Braiding',
        'is_price_variable': true,
        'track_stock': false,
        'stock': <Object?>[],
      });

      await tester.pumpWidget(wrap(sheetOpener(item)));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Add to cart'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(cart().lines, isEmpty);
    });

    testWidgets('two different prices stay two lines', (tester) async {
      final item = itemFrom(const {
        'name': 'Braiding',
        'is_price_variable': true,
        'track_stock': false,
        'stock': <Object?>[],
      });

      await tester.pumpWidget(wrap(sheetOpener(item)));

      for (final typed in ['450', '600']) {
        await tester.tap(find.text('open'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('Add to cart'));
        await tester.pumpAndSettle();
        await tester.enterText(find.byType(TextField), typed);
        await tester.tap(find.text('Add'));
        await tester.pumpAndSettle();
      }

      // Two prices for one thing are two decisions somebody made. Merging them
      // would keep one quantity and silently discard a price that was charged.
      expect(cart().lines, hasLength(2));
      expect(
        cart().lines.map((line) => line.unitPriceCents),
        containsAll(<int>[45000, 60000]),
      );
    });

    testWidgets('a price of nothing is refused', (tester) async {
      final item = itemFrom(const {
        'name': 'Braiding',
        'is_price_variable': true,
        'track_stock': false,
        'stock': <Object?>[],
      });

      await tester.pumpWidget(wrap(sheetOpener(item)));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Add to cart'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '0');
      await tester.tap(find.text('Add'));
      await tester.pumpAndSettle();

      expect(cart().lines, isEmpty, reason: 'a zero-price line is a giveaway');
    });
  });
}
