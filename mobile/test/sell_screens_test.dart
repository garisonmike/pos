/// The screens a cashier uses with a customer standing there.
///
/// These test what is on the glass, because the mistakes that matter here are
/// interface mistakes: a Finish button that is live when the cash is short, a
/// temporary reference that reads like a real receipt number, an offline
/// warning that a cashier can miss.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/api_client.dart';
import 'package:pos_till/data/cart/cart_controller.dart';
import 'package:pos_till/data/cart/checkout_service.dart';
import 'package:pos_till/data/cart/pricing.dart';
import 'package:pos_till/data/printing/escpos.dart';
import 'package:pos_till/features/sell/cart_screen.dart';
import 'package:pos_till/features/sell/connectivity.dart';
import 'package:pos_till/features/sell/sale_done_screen.dart';
import 'package:pos_till/features/sell/tender_screen.dart';

LineInput sugar({int quantityMilli = 1000, int price = 18000}) => LineInput(
      itemId: 'sugar',
      name: 'Sugar 1kg',
      unitPriceCents: price,
      quantityMilli: quantityMilli,
      taxRateBps: 1600,
    );

CheckoutResult resultFor({
  CheckoutOutcome outcome = CheckoutOutcome.settled,
  int changeCents = 2000,
  int? receiptNumber = 7,
  String? provisionalReference,
  ApiException? error,
}) =>
    CheckoutResult(
      outcome: outcome,
      clientUuid: 'uuid-1',
      totals: priceCart([sugar()]),
      tenderedCents: 20000,
      changeCents: changeCents,
      receiptNumber: receiptNumber,
      provisionalReference: provisionalReference,
      error: error,
    );

Widget host(Widget child, {List<Override> overrides = const []}) =>
    ProviderScope(
      overrides: overrides,
      child: MaterialApp(home: child),
    );

/// A cart pre-loaded into the provider a screen reads from.
List<Override> cartWith(void Function(CartController) build) {
  final controller = CartController();
  build(controller);
  return [
    cartControllerProvider.overrideWith((ref) => controller),
  ];
}

void main() {
  // The till runs on a tablet. Testing these screens in the default 800x600
  // window would be testing a device nobody uses, and would either fail on
  // layout or quietly assert against a cramped variant of the real thing.
  setUp(() {
    final view = TestWidgetsFlutterBinding.ensureInitialized().platformDispatcher
        .views.first;
    view.physicalSize = const Size(1200, 1920);
    view.devicePixelRatio = 1.0;
  });

  tearDown(() {
    final view = TestWidgetsFlutterBinding.ensureInitialized().platformDispatcher
        .views.first;
    view.resetPhysicalSize();
    view.resetDevicePixelRatio();
  });

  group('the cart screen', () {
    testWidgets('an empty cart says so rather than showing a zero total',
        (tester) async {
      await tester.pumpWidget(host(const CartScreen()));

      expect(find.text('Nothing scanned yet'), findsOneWidget);
      expect(find.textContaining('Take payment'), findsNothing);
    });

    testWidgets('the total shown is the rounded figure the cashier will ask for',
        (tester) async {
      // Showing the exact total and then taking a different amount is how a
      // drawer ends the day a few shillings out with nobody able to say why.
      await tester.pumpWidget(
        host(
          const CartScreen(),
          overrides: cartWith((cart) => cart.add(sugar(price: 18049))),
        ),
      );

      // The line still shows its real price - 180.49 is what the item costs.
      // What must be rounded is the figure the cashier asks for.
      final total = tester.widget<Text>(
        find.descendant(
          of: find.byType(FilledButton),
          matching: find.textContaining('Take payment'),
        ),
      );
      expect(total.data, contains('KES 180.00'));
    });

    testWidgets('rounding is named rather than silently absorbed',
        (tester) async {
      await tester.pumpWidget(
        host(
          const CartScreen(),
          overrides: cartWith((cart) => cart.add(sugar(price: 18049))),
        ),
      );

      expect(find.text('Rounding'), findsOneWidget);
    });

    testWidgets('an unapproved discount blocks the payment button',
        (tester) async {
      await tester.pumpWidget(
        host(
          const CartScreen(),
          overrides: cartWith((cart) {
            cart.add(sugar());
            cart.discountCart(bps: 1000, reason: 'Damaged');
          }),
        ),
      );

      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Take payment  KES 162.00'),
      );
      expect(button.onPressed, isNull);
      expect(find.textContaining('manager has to approve'), findsOneWidget);
    });

    testWidgets('an approved discount unblocks it', (tester) async {
      await tester.pumpWidget(
        host(
          const CartScreen(),
          overrides: cartWith((cart) {
            cart.add(sugar());
            cart.discountCart(bps: 1000, reason: 'Damaged');
            cart.approve(
              CartApproval(
                username: 'grace',
                reason: 'Damaged',
                method: 'PIN',
                at: DateTime(2026, 8, 14),
              ),
            );
          }),
        ),
      );

      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Take payment  KES 162.00'),
      );
      expect(button.onPressed, isNotNull);
    });
  });

  group('the tender screen', () {
    Widget tenderHost({
      required Future<CheckoutResult> Function({
        required CartState cart,
        required int tenderedCents,
      }) onCheckout,
      int price = 18000,
    }) =>
        host(
          TenderScreen(onCheckout: onCheckout),
          overrides: cartWith((cart) => cart.add(sugar(price: price))),
        );

    testWidgets('finishing is refused while the cash is short', (tester) async {
      await tester.pumpWidget(
        tenderHost(onCheckout: ({required cart, required tenderedCents}) async =>
            resultFor()),
      );

      expect(find.text('Not enough'), findsOneWidget);
      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Not enough'),
      );
      expect(button.onPressed, isNull);
    });

    testWidgets('the shortfall is shown, not a change figure of zero',
        (tester) async {
      await tester.pumpWidget(
        tenderHost(onCheckout: ({required cart, required tenderedCents}) async =>
            resultFor()),
      );

      expect(find.text('Still to pay'), findsOneWidget);
      expect(find.text('Change'), findsNothing);
    });

    testWidgets('the exact-amount button settles the sale in one tap',
        (tester) async {
      await tester.pumpWidget(
        tenderHost(onCheckout: ({required cart, required tenderedCents}) async =>
            resultFor()),
      );

      await tester.tap(find.text('Exact'));
      await tester.pump();

      expect(find.text('Change'), findsOneWidget);
      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Finish sale'),
      );
      expect(button.onPressed, isNotNull);
    });

    testWidgets('change is worked out against the rounded figure',
        (tester) async {
      await tester.pumpWidget(
        tenderHost(
          price: 18049,
          onCheckout: ({required cart, required tenderedCents}) async =>
              resultFor(),
        ),
      );

      // 180.49 rounds to 180.00, so a 200 note leaves 20.00.
      await tester.tap(find.text('200.00'));
      await tester.pump();

      expect(find.text('KES 20.00'), findsOneWidget);
    });

    testWidgets('the keypad builds an amount in cents', (tester) async {
      await tester.pumpWidget(
        tenderHost(onCheckout: ({required cart, required tenderedCents}) async =>
            resultFor()),
      );

      for (final digit in ['2', '0', '0', '0', '0']) {
        await tester.tap(find.widgetWithText(OutlinedButton, digit).first);
        await tester.pump();
      }

      // 2 0 0 0 0 reads as 200.00, not 20000.00.
      expect(find.text('KES 200.00'), findsWidgets);
    });

    testWidgets('a refused sale keeps the cart so it can be fixed',
        (tester) async {
      await tester.pumpWidget(
        tenderHost(
          onCheckout: ({required cart, required tenderedCents}) async =>
              resultFor(
            outcome: CheckoutOutcome.refused,
            error: const ApiException(
              status: 400,
              code: 'item_not_found',
              message: 'No such item.',
            ),
          ),
        ),
      );

      await tester.tap(find.text('Exact'));
      await tester.pump();
      await tester.tap(find.text('Finish sale'));
      await tester.pumpAndSettle();

      expect(find.text('The sale was not accepted'), findsOneWidget);
      expect(find.text('No such item.'), findsOneWidget);
    });
  });

  group('the offline banner', () {
    testWidgets('nothing is shown when all is well', (tester) async {
      await tester.pumpWidget(host(const Scaffold(body: OfflineBanner())));

      expect(find.byType(Container), findsNothing);
    });

    testWidgets('being offline tells the cashier to keep selling',
        (tester) async {
      final controller = ConnectivityController()..recordOffline();
      await tester.pumpWidget(
        host(
          const Scaffold(body: OfflineBanner()),
          overrides: [connectivityProvider.overrideWith((ref) => controller)],
        ),
      );

      expect(find.textContaining('No connection'), findsOneWidget);
      expect(find.textContaining('Keep selling'), findsOneWidget);
    });

    testWidgets('a backlog is counted, not described as "some"',
        (tester) async {
      final controller = ConnectivityController()
        ..setBacklog(queued: 7, stuck: 0);
      await tester.pumpWidget(
        host(
          const Scaffold(body: OfflineBanner()),
          overrides: [connectivityProvider.overrideWith((ref) => controller)],
        ),
      );

      expect(find.textContaining('7 waiting to send'), findsOneWidget);
    });

    testWidgets('stuck sales outrank a mere backlog', (tester) async {
      // A queue drains itself; a refusal does not. Showing the gentler message
      // would let real money sit unnoticed.
      final controller = ConnectivityController()
        ..recordOffline()
        ..setBacklog(queued: 7, stuck: 2);
      await tester.pumpWidget(
        host(
          const Scaffold(body: OfflineBanner()),
          overrides: [connectivityProvider.overrideWith((ref) => controller)],
        ),
      );

      expect(find.textContaining('2 sales need checking'), findsOneWidget);
      expect(find.textContaining('waiting to send'), findsNothing);
    });

    testWidgets('a refusal does not put the till into the offline state',
        (tester) async {
      // A 403 proves the server is reachable. Showing "offline" would send a
      // cashier hunting for a network fault that is not there.
      final controller = ConnectivityController()
        ..recordFailure(
          const ApiException(
            status: 403,
            code: 'discount_authorization_required',
            message: 'Needs a manager.',
          ),
        );

      expect(controller.state.isOffline, isFalse);
    });
  });

  group('the finished-sale screen', () {
    testWidgets('change is the largest thing on the screen', (tester) async {
      await tester.pumpWidget(
        host(
          SaleDoneScreen(
            result: resultFor(changeCents: 2000),
            printer: InMemoryPrinter(),
            buildReceipt: _receiptFor,
          ),
        ),
      );

      final change = tester.widget<Text>(find.text('KES 20.00'));
      expect(change.style!.fontSize, 48);
    });

    testWidgets('a settled sale shows its receipt number', (tester) async {
      await tester.pumpWidget(
        host(
          SaleDoneScreen(
            result: resultFor(),
            printer: InMemoryPrinter(),
            buildReceipt: _receiptFor,
          ),
        ),
      );

      expect(find.text('Receipt #7'), findsOneWidget);
    });

    testWidgets('a queued sale says its reference is temporary',
        (tester) async {
      await tester.pumpWidget(
        host(
          SaleDoneScreen(
            result: resultFor(
              outcome: CheckoutOutcome.queued,
              receiptNumber: null,
              provisionalReference: 'TMP-00007',
            ),
            printer: InMemoryPrinter(),
            buildReceipt: _receiptFor,
          ),
        ),
      );

      expect(find.textContaining('Temporary reference'), findsOneWidget);
      expect(find.textContaining('gets its real receipt number'), findsOneWidget);
    });

    testWidgets('printing sends the job', (tester) async {
      final printer = InMemoryPrinter();
      await tester.pumpWidget(
        host(
          SaleDoneScreen(
            result: resultFor(),
            printer: printer,
            buildReceipt: _receiptFor,
          ),
        ),
      );

      await tester.tap(find.text('Print receipt'));
      await tester.pumpAndSettle();

      expect(printer.jobs, hasLength(1));
      expect(find.text('Printed.'), findsOneWidget);
    });

    testWidgets('no printer does not look like a failed sale', (tester) async {
      final printer = InMemoryPrinter()..available = false;
      await tester.pumpWidget(
        host(
          SaleDoneScreen(
            result: resultFor(),
            printer: printer,
            buildReceipt: _receiptFor,
          ),
        ),
      );

      await tester.tap(find.text('Print receipt'));
      await tester.pumpAndSettle();

      expect(find.textContaining('The sale is saved'), findsOneWidget);
      expect(find.text('Sale complete'), findsOneWidget);
    });
  });
}

PrintableReceipt _receiptFor(CheckoutResult result) => PrintableReceipt(
      businessName: 'Mama Njeri Duka',
      lines: const [
        PrintableLine(
          name: 'Sugar 1kg',
          quantityMilli: 1000,
          unitPriceCents: 18000,
          grossCents: 18000,
        ),
      ],
      subtotalCents: result.totals.subtotalCents,
      taxCents: result.totals.taxCents,
      totalCents: result.totals.totalCents,
      tenderedCents: result.tenderedCents,
      changeCents: result.changeCents,
      receiptNumber: result.receiptNumber,
      provisionalReference: result.provisionalReference,
    );
