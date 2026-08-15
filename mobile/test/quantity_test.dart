/// Selling by weight at the counter.
///
/// A cashier scooping sugar reads a figure off a scale and types it. Stepping
/// by whole kilos cannot express 0.35 kg, and 0.35 kg is an ordinary purchase.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/cart/cart_controller.dart';
import 'package:pos_till/data/cart/pricing.dart';
import 'package:pos_till/features/sell/cart_screen.dart';
import 'package:pos_till/features/sell/quantity_sheet.dart';

LineInput sugarByTheKilo({int quantityMilli = 1000}) => LineInput(
      itemId: 'sugar',
      name: 'Sugar (loose)',
      unitPriceCents: 18000,
      quantityMilli: quantityMilli,
      unit: 'KG',
      taxRateBps: 1600,
    );

LineInput sweetEach() => const LineInput(
      itemId: 'sweet',
      name: 'Sweet',
      unitPriceCents: 200,
      quantityMilli: 1000,
      unit: 'EACH',
    );

Widget host(Widget child) => MaterialApp(home: child);

List<Override> cartWith(void Function(CartController) build) {
  final controller = CartController();
  build(controller);
  return [cartControllerProvider.overrideWith((ref) => controller)];
}

void main() {
  setUp(() {
    final view =
        TestWidgetsFlutterBinding.ensureInitialized().platformDispatcher.views.first;
    view.physicalSize = const Size(1200, 2400);
    view.devicePixelRatio = 1.0;
  });

  tearDown(() {
    final view =
        TestWidgetsFlutterBinding.ensureInitialized().platformDispatcher.views.first;
    view.resetPhysicalSize();
    view.resetDevicePixelRatio();
  });

  group('reading a unit', () {
    test('the terse code becomes something a cashier reads', () {
      expect(unitLabel('KG'), 'kg');
      expect(unitLabel('L'), 'L');
      expect(unitLabel('ML'), 'ml');
      expect(unitLabel('HOUR'), 'hr');
    });

    test('a piece has no unit to show', () {
      expect(unitLabel('EACH'), '');
      expect(isCountedEach('EACH'), isTrue);
    });

    test('an unknown unit is treated as a piece rather than crashing', () {
      // A server that adds a unit this build has not heard of should degrade
      // to the ordinary stepper, not take the cart down.
      expect(isCountedEach('FURLONG'), isTrue);
    });

    test('thousandths read as a person writes them', () {
      expect(formatQuantity(1000), '1');
      expect(formatQuantity(2500), '2.5');
      expect(formatQuantity(333), '0.333');
      expect(formatQuantity(350), '0.35');
      expect(formatQuantity(0), '0');
    });
  });

  group('the quantity sheet', () {
    Future<void> pump(WidgetTester tester, {int initial = 1000}) async {
      await tester.pumpWidget(
        host(
          Scaffold(
            body: QuantitySheet(
              itemName: 'Sugar (loose)',
              unit: 'KG',
              unitPriceCents: 18000,
              initialMilli: initial,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('it opens showing the quantity already on the line',
        (tester) async {
      // A cashier adjusting an existing line should see what is there, not a
      // zero they have to retype.
      await pump(tester, initial: 2500);

      expect(find.text('2.5 kg'), findsWidgets);
    });

    testWidgets('the keypad builds thousandths', (tester) async {
      await pump(tester);

      for (final digit in ['3', '5', '0']) {
        await tester.tap(find.widgetWithText(OutlinedButton, digit).first);
        await tester.pump();
      }

      // 3 5 0 reads as 0.35 kg, not 350 kg.
      expect(find.text('0.35 kg'), findsWidgets);
    });

    testWidgets('it prices as it goes, the way the server will', (tester) async {
      await pump(tester);

      for (final digit in ['5', '0', '0']) {
        await tester.tap(find.widgetWithText(OutlinedButton, digit).first);
        await tester.pump();
      }

      // 0.5 kg at KES 180.00 is KES 90.00.
      expect(find.text('KES 90.00'), findsOneWidget);
    });

    testWidgets('an awkward weight resolves its cent the same way', (tester) async {
      // 0.333 kg at 180.00 is 5,994 cents - the figure the server's own
      // pricing test pins. Rounded half-up at the line, once.
      await pump(tester);

      for (final digit in ['3', '3', '3']) {
        await tester.tap(find.widgetWithText(OutlinedButton, digit).first);
        await tester.pump();
      }

      expect(find.text('KES 59.94'), findsOneWidget);
    });

    testWidgets('a common quantity is one tap', (tester) async {
      await pump(tester);

      await tester.tap(find.widgetWithText(OutlinedButton, '0.25 kg'));
      await tester.pump();

      expect(find.text('0.25 kg'), findsWidgets);
      expect(find.text('KES 45.00'), findsOneWidget);
    });

    testWidgets('backspace removes a digit', (tester) async {
      await pump(tester);

      for (final digit in ['1', '2', '3']) {
        await tester.tap(find.widgetWithText(OutlinedButton, digit).first);
        await tester.pump();
      }
      await tester.tap(find.widgetWithText(OutlinedButton, '⌫'));
      await tester.pump();

      expect(find.text('0.012 kg'), findsWidgets);
    });

    testWidgets('clear takes it to zero', (tester) async {
      await pump(tester);

      await tester.tap(find.widgetWithText(OutlinedButton, '5').first);
      await tester.pump();
      await tester.tap(find.widgetWithText(OutlinedButton, 'C'));
      await tester.pump();

      expect(find.text('0 kg'), findsWidgets);
    });

    testWidgets('a zero quantity cannot be added', (tester) async {
      // Not a sale. Refused here so a cashier fixes it with the customer still
      // there, rather than reading a server error afterwards.
      await pump(tester);

      await tester.tap(find.widgetWithText(OutlinedButton, 'C'));
      await tester.pump();

      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Enter a quantity'),
      );
      expect(button.onPressed, isNull);
    });

    testWidgets('the confirm button names what it will add', (tester) async {
      await pump(tester);

      await tester.tap(find.widgetWithText(OutlinedButton, '0.5 kg'));
      await tester.pump();

      expect(find.text('Add 0.5 kg'), findsOneWidget);
    });

    testWidgets('the price per unit is shown', (tester) async {
      await pump(tester);

      expect(find.text('KES 180.00 per kg'), findsOneWidget);
    });
  });

  group('the cart row', () {
    testWidgets('a measured line shows its unit', (tester) async {
      // "2.5 × KES 180.00" says nothing about whether that is kilos or bags,
      // and the two are a very different amount of sugar.
      await tester.pumpWidget(
        host(
          ProviderScope(
            overrides: cartWith((cart) => cart.add(sugarByTheKilo(quantityMilli: 2500))),
            child: const CartScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('2.5 kg × KES 180.00 / kg'), findsOneWidget);
    });

    testWidgets('a piece shows no unit', (tester) async {
      await tester.pumpWidget(
        host(
          ProviderScope(
            overrides: cartWith((cart) => cart.add(sweetEach())),
            child: const CartScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('1 × KES 2.00'), findsOneWidget);
      expect(find.textContaining('/ '), findsNothing);
    });

    testWidgets('a measured line offers the keypad, not steppers',
        (tester) async {
      // Stepping to 0.35 kg a thousandth at a time is not a thing anybody
      // would do.
      await tester.pumpWidget(
        host(
          ProviderScope(
            overrides: cartWith((cart) => cart.add(sugarByTheKilo())),
            child: const CartScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.add), findsNothing);
      expect(find.byIcon(Icons.remove), findsNothing);
      expect(find.widgetWithText(TextButton, '1 kg'), findsOneWidget);
    });

    testWidgets('a piece keeps its steppers', (tester) async {
      await tester.pumpWidget(
        host(
          ProviderScope(
            overrides: cartWith((cart) => cart.add(sweetEach())),
            child: const CartScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.add), findsOneWidget);
      expect(find.byIcon(Icons.remove), findsOneWidget);
    });

    testWidgets('typing a weight updates the line and the total',
        (tester) async {
      final controller = CartController()..add(sugarByTheKilo());
      await tester.pumpWidget(
        host(
          ProviderScope(
            overrides: [cartControllerProvider.overrideWith((ref) => controller)],
            child: const CartScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.widgetWithText(TextButton, '1 kg'));
      await tester.pumpAndSettle();

      await tester.tap(find.widgetWithText(OutlinedButton, '0.25 kg'));
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, 'Add 0.25 kg'));
      await tester.pumpAndSettle();

      expect(controller.state.lines.single.quantityMilli, 250);
      // 0.25 kg at 180.00 is KES 45.00.
      expect(controller.state.totals.totalCents, 4500);
    });

    testWidgets('dismissing the sheet leaves the line alone', (tester) async {
      final controller = CartController()..add(sugarByTheKilo(quantityMilli: 2000));
      await tester.pumpWidget(
        host(
          ProviderScope(
            overrides: [cartControllerProvider.overrideWith((ref) => controller)],
            child: const CartScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.widgetWithText(TextButton, '2 kg'));
      await tester.pumpAndSettle();
      // Tap outside the sheet to dismiss it.
      await tester.tapAt(const Offset(10, 10));
      await tester.pumpAndSettle();

      expect(controller.state.lines.single.quantityMilli, 2000);
    });
  });

  group('pricing a fractional quantity', () {
    test('the sheet and the pricer agree', () {
      // The figure a cashier reads before confirming must be the one that ends
      // up on the receipt.
      for (final milli in [250, 333, 350, 500, 1125, 2500]) {
        final totals = priceCart([sugarByTheKilo(quantityMilli: milli)]);
        final sheetFigure = (2 * 18000 * milli + 1000) ~/ 2000;

        expect(
          totals.lines.single.grossCents,
          sheetFigure,
          reason: 'at $milli thousandths',
        );
      }
    });

    test('a weighed line still totals to its parts', () {
      final totals = priceCart([sugarByTheKilo(quantityMilli: 333)]);

      expect(totals.lines.single.netCents + totals.lines.single.taxCents,
          totals.lines.single.grossCents);
      expect(totals.totalCents, 5994);
    });
  });
}
