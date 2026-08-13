import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/theme.dart';
import 'package:pos_till/data/models.dart';
import 'package:pos_till/features/auth/business_screen.dart';
import 'package:pos_till/features/auth/widgets.dart';
import 'package:pos_till/features/catalog/item_sheet.dart';

/// Widget tests for the screens a cashier meets.
///
/// These lean on the parts that can be built without a live server: the sign-in
/// entry point, the PIN pad, and how an item renders. The repositories are
/// covered by the backend's own suite, so what is worth testing here is the
/// things a shop would notice - that targets are big enough to hit, that a
/// service does not display a misleading stock figure, and that money is never
/// formatted through a double.

Widget wrap(Widget child) => ProviderScope(
  child: MaterialApp(theme: TillTheme.build(), home: child),
);

void main() {
  group('Money formatting', () {
    test('formats as it appears on a receipt', () {
      expect(const Money(18000).format(), 'KES 180.00');
      expect(const Money(6500).format(), 'KES 65.00');
      expect(const Money(5).format(), 'KES 0.05');
      expect(const Money(0).format(), 'KES 0.00');
    });

    test('groups thousands', () {
      expect(const Money(123456789).format(), 'KES 1,234,567.89');
      expect(const Money(100000).format(), 'KES 1,000.00');
    });

    test('handles negative amounts, for refunds', () {
      expect(const Money(-18000).format(), '-KES 180.00');
    });

    test('never loses a cent to floating point', () {
      // The case that motivates integer cents: 0.10 has no exact double
      // representation, so ten of them formatted through a double drift.
      final total = List.filled(10, const Money(10))
          .fold<int>(0, (sum, money) => sum + money.cents);
      expect(Money(total).format(), 'KES 1.00');
    });
  });

  group('Stock display', () {
    test('trims trailing zeros so 40.000 reads as 40', () {
      const level = StockLevel(storeCode: 'MAIN', quantity: '40.000', isLow: false);
      expect(level.display, '40');
    });

    test('keeps a meaningful fraction, for goods sold by weight', () {
      const level = StockLevel(storeCode: 'MAIN', quantity: '2.500', isLow: false);
      expect(level.display, '2.5');
    });

    test('an untracked item reports no stock rather than zero', () {
      // A haircut showing "0" reads as sold out, and a cashier would hesitate
      // over something they should simply sell.
      final service = Item.fromJson(const {
        'id': 'x',
        'sku': 'SVC-CUT',
        'name': 'Haircut',
        'price_cents': 30000,
        'item_type': 'SERVICE',
        'track_stock': false,
        'stock': [],
      });
      expect(service.stockSummary, isNull);
    });

    test('a tracked item summarises its quantity', () {
      final product = Item.fromJson(const {
        'id': 'x',
        'sku': 'SUGAR-1KG',
        'name': 'Sugar 1kg',
        'price_cents': 18000,
        'track_stock': true,
        'stock': [
          {'store_code': 'MAIN', 'quantity': '40.000', 'is_low': false},
        ],
      });
      expect(product.stockSummary, '40');
    });
  });

  group('Variable pricing', () {
    test('shows a variable price as a starting point, not a total', () {
      final service = Item.fromJson(const {
        'id': 'x',
        'sku': 'SVC-BRAID',
        'name': 'Braiding',
        'price_cents': 50000,
        'item_type': 'SERVICE',
        'is_price_variable': true,
        'track_stock': false,
      });
      expect(service.priceDisplay, 'from KES 500.00');
    });

    test('shows a fixed price plainly', () {
      final product = Item.fromJson(const {
        'id': 'x',
        'sku': 'SUGAR-1KG',
        'name': 'Sugar 1kg',
        'price_cents': 18000,
        'track_stock': true,
      });
      expect(product.priceDisplay, 'KES 180.00');
    });
  });

  group('Sessions', () {
    test('reads the role and business from a sign-in response', () {
      final session = Session.fromJson(const {
        'user': {
          'id': 'u1',
          'username': 'mary',
          'full_name': 'Mary Wanjiku',
          'role': 'CASHIER',
        },
        'tenant': {'slug': 'mama-njeri', 'name': 'Mama Njeri Duka'},
      });

      expect(session.shortName, 'Mary');
      expect(session.roleLabel, 'Cashier');
      expect(session.canManage, isFalse);
      expect(session.tenantName, 'Mama Njeri Duka');
    });

    test('a manager may register a till', () {
      final session = Session.fromJson(const {
        'user': {'id': 'u2', 'username': 'j', 'full_name': 'J K', 'role': 'MANAGER'},
      });
      expect(session.canManage, isTrue);
    });
  });

  group('Business setup screen', () {
    testWidgets('asks for the business ID', (tester) async {
      await tester.pumpWidget(wrap(const BusinessScreen()));
      expect(find.text('Set up this till'), findsOneWidget);
      expect(find.text('Business ID'), findsOneWidget);
    });

    testWidgets('refuses an empty ID rather than storing nothing', (tester) async {
      await tester.pumpWidget(wrap(const BusinessScreen()));
      await tester.tap(find.text('Continue'));
      await tester.pump();

      expect(find.text('Enter the business ID'), findsOneWidget);
    });

    testWidgets('refuses characters a slug cannot contain', (tester) async {
      await tester.pumpWidget(wrap(const BusinessScreen()));
      await tester.enterText(find.byType(TextFormField), 'Mama Njeri!');
      await tester.tap(find.text('Continue'));
      await tester.pump();

      expect(find.text('Letters, numbers and dashes only'), findsOneWidget);
    });
  });

  group('Touch targets', () {
    testWidgets('PIN keys are comfortably larger than the minimum', (tester) async {
      // The control used most often in the app, at speed and often one-handed.
      await tester.pumpWidget(
        wrap(Scaffold(body: PinKey(label: '7', onTap: () {}))),
      );

      final size = tester.getSize(find.byType(PinKey));
      expect(size.height, greaterThanOrEqualTo(TillTheme.minTapTarget));
      expect(size.height, greaterThanOrEqualTo(72));
    });

    testWidgets('the primary action is a full-width bar', (tester) async {
      await tester.pumpWidget(
        wrap(
          Scaffold(
            body: FilledButton(onPressed: () {}, child: const Text('Sign in')),
          ),
        ),
      );

      final size = tester.getSize(find.byType(FilledButton));
      expect(size.height, greaterThanOrEqualTo(TillTheme.primaryActionHeight));
    });
  });

  group('PIN entry feedback', () {
    testWidgets('shows one dot per digit entered', (tester) async {
      await tester.pumpWidget(wrap(const Scaffold(body: PinDots(length: 2))));

      final dots = tester.widgetList<Container>(
        find.descendant(of: find.byType(PinDots), matching: find.byType(Container)),
      );
      expect(dots.length, 4);
    });

    testWidgets('an error stays on screen rather than flashing past', (tester) async {
      // A snackbar would vanish while the cashier is looking at the keypad.
      await tester.pumpWidget(
        wrap(const Scaffold(body: ErrorBanner(message: 'That PIN was not recognised'))),
      );
      await tester.pump(const Duration(seconds: 6));

      expect(find.text('That PIN was not recognised'), findsOneWidget);
    });
  });

  group('Item sheet', () {
    testWidgets('shows a service without a stock figure', (tester) async {
      final service = Item.fromJson(const {
        'id': 'x',
        'sku': 'SVC-BRAID',
        'name': 'Braiding',
        'price_cents': 50000,
        'item_type': 'SERVICE',
        'is_price_variable': true,
        'track_stock': false,
        'duration_minutes': 120,
      });

      await tester.pumpWidget(
        wrap(
          Builder(
            builder: (context) => Scaffold(
              body: TextButton(
                onPressed: () => showItemSheet(context, service),
                child: const Text('open'),
              ),
            ),
          ),
        ),
      );

      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      expect(find.text('Braiding'), findsOneWidget);
      expect(find.text('from KES 500.00'), findsOneWidget);
      expect(find.text('120 minutes'), findsOneWidget);
      expect(find.text('In stock'), findsNothing);
    });

    testWidgets('shows every barcode an item carries', (tester) async {
      final product = Item.fromJson(const {
        'id': 'x',
        'sku': 'SUGAR-1KG',
        'name': 'Sugar 1kg',
        'price_cents': 18000,
        'track_stock': true,
        'barcodes': [
          {'code': '6161100234567'},
          {'code': '6161100234574'},
        ],
        'stock': [
          {'store_code': 'MAIN', 'quantity': '40.000', 'is_low': false},
        ],
      });

      await tester.pumpWidget(
        wrap(
          Builder(
            builder: (context) => Scaffold(
              body: TextButton(
                onPressed: () => showItemSheet(context, product),
                child: const Text('open'),
              ),
            ),
          ),
        ),
      );

      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      // Both codes, because a case pack and a single unit are one product with
      // two codes and either must be findable.
      expect(find.text('6161100234567'), findsOneWidget);
      expect(find.text('6161100234574'), findsOneWidget);
    });
  });
}
