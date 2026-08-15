/// The reports and drawer screens.
///
/// Three things the backend protects that a screen could quietly undo, and
/// which most of this file is about:
///
/// * cashier rates rendered without their denominators;
/// * a variance and its explanation folded into one number;
/// * a missing connection rendered as a zero.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/api_client.dart';
import 'package:pos_till/data/reports/models.dart';
import 'package:pos_till/data/reports/reports_repository.dart';
import 'package:pos_till/features/reports/drawer_screen.dart';
import 'package:pos_till/features/reports/reports_screen.dart';
import 'package:pos_till/features/shift/shift_screen.dart';

/// A transport that answers with whatever the test sets.
class FakeTransport implements ReportsTransport {
  final List<String> paths = [];
  final Map<String, Map<String, dynamic>> replies = {};
  ApiException? throwThis;

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, dynamic>? query}) async {
    paths.add(path);
    if (throwThis != null) throw throwThis!;
    return replies[path] ?? const {};
  }

  @override
  Future<Map<String, dynamic>> post(String path, {Map<String, dynamic>? body}) async {
    paths.add(path);
    if (throwThis != null) throw throwThis!;
    return replies[path] ?? const {};
  }
}

const offline = ApiException(
  status: 0,
  code: 'offline',
  message: 'No connection to the server.',
);

Map<String, dynamic> salesReply({int cash = 18000, int mpesa = 0}) => {
      'periods': [
        {
          'label': '2026-08-15',
          'sale_count': 2,
          'gross_cents': cash + mpesa,
          'net_cents': 15517,
          'tax_cents': 2483,
          'discount_cents': 0,
          'taken': {
            'cash_cents': cash,
            'mpesa_cents': mpesa,
            'total_cents': cash + mpesa,
          },
          'refunded': {'cash_cents': 0, 'mpesa_cents': 0, 'total_cents': 0},
          'refund_count': 0,
          'net_taken_cents': cash + mpesa,
          'average_basket_cents': 9000,
          'refund_rate_bps': 0,
          'void_count': 0,
          'offline_sale_count': 0,
        },
      ],
    };

Map<String, dynamic> drawerReply({
  int variance = 0,
  int lateCount = 0,
  int lateCash = 0,
  int? explained,
}) =>
    {
      'label': '2026-08-15',
      'shifts': [
        {
          'shift_id': 'abcdef123456',
          'cashier': 'mary',
          'store_code': 'MAIN',
          'state': 'CLOSED',
          'counted': {
            'opening_float_cents': 100000,
            'declared_closing_cents': 118000,
            'expected_closing_cents': 118000 - variance,
            'variance_cents': variance,
          },
          'arrived_after_close': {
            'count': lateCount,
            'cash_cents': lateCash,
            'payments': [
              for (var i = 0; i < lateCount; i++)
                {'sale_id': 'sale-$i', 'amount_cents': lateCash, 'method': 'CASH'},
            ],
          },
          'explained_variance_cents': explained,
          'is_reconciled': lateCount == 0,
        },
      ],
      'cash_taken_in_period_cents': 36000,
      'unreconciled_shift_count': lateCount > 0 ? 1 : 0,
      'note': 'Counted figures are frozen at close and are never recomputed. '
          'Anything that arrived afterwards is listed beside them, not added '
          'into them.',
    };

Widget host(Widget child) => MaterialApp(home: child);

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

  group('reports are online-only', () {
    test('an unreachable server raises its own type, not an empty report',
        () async {
      // A screen that received an empty report would render zeroes, and a
      // manager would read those as a quiet day.
      final transport = FakeTransport()..throwThis = offline;
      final repository = ReportsRepository(transport);

      expect(
        () => repository.sales(),
        throwsA(isA<ReportsUnavailable>()),
      );
    });

    test('a server error is unavailability too', () async {
      final transport = FakeTransport()
        ..throwThis = const ApiException(
          status: 500,
          code: 'error',
          message: 'Something went wrong.',
        );

      expect(
        () => ReportsRepository(transport).sales(),
        throwsA(isA<ReportsUnavailable>()),
      );
    });

    test('a refusal is not unavailability', () async {
      // A 403 proves the server is reachable. Dressing it up as "no
      // connection" would send a manager after a network fault that is not
      // there.
      final transport = FakeTransport()
        ..throwThis = const ApiException(
          status: 403,
          code: 'permission_denied',
          message: 'Not allowed.',
        );

      expect(
        () => ReportsRepository(transport).sales(),
        throwsA(isA<ApiException>().having((e) => e.status, 'status', 403)),
      );
    });

    testWidgets('the screen says so rather than showing nothing',
        (tester) async {
      final transport = FakeTransport()..throwThis = offline;
      await tester.pumpWidget(
        host(ReportsScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('No connection'), findsOneWidget);
      expect(find.textContaining('This is not a zero'), findsOneWidget);
    });

    testWidgets('it says selling carries on', (tester) async {
      // Otherwise a manager reads "reports unavailable" as "the till is down".
      final transport = FakeTransport()..throwThis = offline;
      await tester.pumpWidget(
        host(ReportsScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Selling carries on'), findsOneWidget);
    });

    testWidgets('no zero figure is rendered anywhere', (tester) async {
      final transport = FakeTransport()..throwThis = offline;
      await tester.pumpWidget(
        host(ReportsScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.text('KES 0.00'), findsNothing);
      expect(find.textContaining('0 sales'), findsNothing);
    });

    testWidgets('it offers a retry', (tester) async {
      final transport = FakeTransport()..throwThis = offline;
      await tester.pumpWidget(
        host(ReportsScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.text('Try again'), findsOneWidget);

      transport.throwThis = null;
      transport.replies['/api/v1/reports/sales/'] = salesReply();
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();

      expect(find.textContaining('No connection'), findsNothing);
      expect(find.text('KES 180.00'), findsWidgets);
    });
  });

  group('the takings tab', () {
    Future<void> pump(WidgetTester tester, FakeTransport transport) async {
      await tester.pumpWidget(
        host(ReportsScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('cash is shown on its own line', (tester) async {
      // It is what reconciles against a drawer somebody counted. The server
      // keeps cash and total apart; one merged figure would undo that.
      final transport = FakeTransport()
        ..replies['/api/v1/reports/sales/'] = salesReply(cash: 18000, mpesa: 5000);

      await pump(tester, transport);

      expect(find.text('Cash'), findsOneWidget);
      expect(find.text('M-Pesa'), findsOneWidget);
      expect(find.text('KES 180.00'), findsWidgets);
      expect(find.text('KES 50.00'), findsWidgets);
    });

    testWidgets('the headline is what was taken', (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/reports/sales/'] = salesReply();

      await pump(tester, transport);

      expect(find.text('Taken'), findsOneWidget);
      expect(find.text('2 sales'), findsOneWidget);
    });

    testWidgets('a rate is rendered from basis points', (tester) async {
      final reply = salesReply();
      (reply['periods'] as List).first['refund_rate_bps'] = 1250;
      final transport = FakeTransport()..replies['/api/v1/reports/sales/'] = reply;

      await pump(tester, transport);

      expect(find.text('12.50%'), findsOneWidget);
    });
  });

  group('cashier figures keep their framing', () {
    Future<void> pump(WidgetTester tester, FakeTransport transport) async {
      await tester.pumpWidget(
        host(ReportsScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cashiers'));
      await tester.pumpAndSettle();
    }

    Map<String, dynamic> cashierReply({int saleCount = 3, int discounted = 2}) => {
          'label': '2026-08-15',
          'cashiers': [
            {
              'username': 'mary',
              'full_name': 'Mary Test',
              'sale_count': saleCount,
              'gross_cents': 54000,
              'discount_cents': 1800,
              'discounted_sale_count': discounted,
              'void_count': 0,
              'refund_count': 0,
              'average_basket_cents': 18000,
              'discount_rate_bps': 333,
              'void_rate_bps': 0,
            },
          ],
          'note': 'Rates are shown beside the counts they come from. A cashier '
              'on a quiet shift will always look different, and a discount '
              'rate says nothing without knowing who authorised each one.',
        };

    testWidgets('the note the server sends is rendered, not dropped',
        (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/reports/sales/'] = salesReply()
        ..replies['/api/v1/reports/cashiers/'] = cashierReply();

      await pump(tester, transport);

      expect(find.textContaining('quiet shift'), findsOneWidget);
      expect(find.textContaining('who authorised'), findsOneWidget);
    });

    testWidgets('a rate never appears without its denominator', (tester) async {
      // "2 of 3" beside the rate, so how thin the evidence is stays visible.
      final transport = FakeTransport()
        ..replies['/api/v1/reports/sales/'] = salesReply()
        ..replies['/api/v1/reports/cashiers/'] = cashierReply();

      await pump(tester, transport);

      expect(find.textContaining('2 of 3'), findsOneWidget);
      expect(find.textContaining('3.33%'), findsOneWidget);
    });

    testWidgets('the sale count is shown too', (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/reports/sales/'] = salesReply()
        ..replies['/api/v1/reports/cashiers/'] = cashierReply();

      await pump(tester, transport);

      expect(find.text('Sales'), findsOneWidget);
    });

    testWidgets('nobody who sold nothing is invented', (tester) async {
      // An absence is not a zero. The server omits them; the screen must not
      // fill the gap back in.
      final reply = cashierReply();
      reply['cashiers'] = [];
      final transport = FakeTransport()
        ..replies['/api/v1/reports/sales/'] = salesReply()
        ..replies['/api/v1/reports/cashiers/'] = reply;

      await pump(tester, transport);

      expect(find.textContaining('Nobody rang anything up'), findsOneWidget);
    });
  });

  group('the drawer tie-out stays visually separate', () {
    Future<void> pump(WidgetTester tester, Map<String, dynamic> reply) async {
      final transport = FakeTransport()..replies['/api/v1/reports/drawers/'] = reply;
      await tester.pumpWidget(
        host(DrawerReportScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('counted and arrived-after-close are separate blocks',
        (tester) async {
      await pump(
        tester,
        drawerReply(variance: 0, lateCount: 1, lateCash: 18000, explained: 18000),
      );

      expect(find.text('COUNTED'), findsOneWidget);
      expect(find.text('ARRIVED AFTER CLOSE'), findsOneWidget);
    });

    testWidgets('the variance shown is the one that was signed for',
        (tester) async {
      // 0.00, not 180.00. The explanation must not have been folded in.
      await pump(
        tester,
        drawerReply(variance: 0, lateCount: 1, lateCash: 18000, explained: 18000),
      );

      expect(find.text('Variance'), findsOneWidget);
      expect(find.text('KES 0.00'), findsOneWidget);
    });

    testWidgets('the explanation is worded as a hypothetical', (tester) async {
      await pump(
        tester,
        drawerReply(variance: 0, lateCount: 1, lateCash: 18000, explained: 18000),
      );

      expect(find.textContaining('Had those landed in time'), findsOneWidget);
      expect(find.textContaining('unchanged'), findsWidgets);
    });

    testWidgets('a short drawer names the direction', (tester) async {
      await pump(tester, drawerReply(variance: -5000));

      expect(find.text('Short by'), findsOneWidget);
    });

    testWidgets('a clean drawer says nothing arrived late', (tester) async {
      await pump(tester, drawerReply(variance: 0));

      expect(find.textContaining('Nothing arrived after'), findsOneWidget);
      expect(find.text('ARRIVED AFTER CLOSE'), findsNothing);
    });

    testWidgets('the period carries the figure the drawers are compared against',
        (tester) async {
      // A manager who does not know this exists will assume the drawers should
      // add up to it exactly.
      await pump(tester, drawerReply(variance: 0, lateCount: 1, lateCash: 18000));

      expect(find.textContaining('Cash taken in this period'), findsOneWidget);
    });

    testWidgets('unreconciled drawers are flagged at the top', (tester) async {
      await pump(
        tester,
        drawerReply(variance: 0, lateCount: 1, lateCash: 18000, explained: 18000),
      );

      expect(find.textContaining('had sales arrive after closing'), findsOneWidget);
    });

    testWidgets('the server note is carried through', (tester) async {
      await pump(tester, drawerReply(variance: 0));

      expect(find.textContaining('frozen at close'), findsOneWidget);
    });

    testWidgets('it has its own unavailable state too', (tester) async {
      final transport = FakeTransport()..throwThis = offline;
      await tester.pumpWidget(
        host(DrawerReportScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('This is not a zero'), findsOneWidget);
    });
  });

  group('parsing keeps the two halves apart', () {
    test('counted and late land in separate fields', () {
      final row = DrawerReconciliation.fromJson(
        (drawerReply(variance: -5000, lateCount: 2, lateCash: 9000, explained: 4000)[
                'shifts'] as List)
            .first as Map<String, dynamic>,
      );

      expect(row.varianceCents, -5000);
      expect(row.lateCashCents, 9000);
      expect(row.explainedVarianceCents, 4000);
      expect(row.isReconciled, isFalse);
    });

    test('an open drawer has no variance at all', () {
      final row = DrawerReconciliation.fromJson({
        'shift_id': 'x',
        'cashier': 'mary',
        'state': 'OPEN',
        'counted': {
          'opening_float_cents': 100000,
          'declared_closing_cents': null,
          'expected_closing_cents': null,
          'variance_cents': null,
        },
        'arrived_after_close': {'count': 0, 'cash_cents': 0, 'payments': []},
        'explained_variance_cents': null,
        'is_reconciled': true,
      });

      expect(row.isOpen, isTrue);
      expect(row.varianceCents, isNull);
      expect(row.explainedVarianceCents, isNull);
    });

    test('a quantity stays a string', () {
      final seller = BestSeller.fromJson({
        'name': 'Sugar',
        'sku': 'S1',
        'quantity': '2.500',
        'revenue_cents': 45000,
        'line_count': 1,
      });

      expect(seller.quantity, '2.500');
      expect(seller.quantityDisplay, '2.5');
    });
  });

  group('the drawer screen', () {
    testWidgets('with no drawer open it offers to open one', (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/shifts/current/'] = {'shift': null};

      await tester.pumpWidget(
        host(ShiftScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.text('Open drawer'), findsOneWidget);
      expect(find.textContaining('Count the cash'), findsOneWidget);
    });

    testWidgets('with one open it asks for a count', (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/shifts/current/'] = {
          'shift': {
            'id': 'shift-1',
            'cashier_username': 'mary',
            'opening_float_cents': 100000,
            'opened_at': '2026-08-15T08:00:00+03:00',
          },
        };

      await tester.pumpWidget(
        host(ShiftScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.text('Close drawer'), findsOneWidget);
      expect(find.textContaining('Count everything in the drawer'), findsOneWidget);
    });

    testWidgets('it never shows what the drawer is expected to hold',
        (tester) async {
      // The blind count. There is nothing to leak - the API sends no expected
      // figure for an open drawer - and this asserts the screen invents none.
      final transport = FakeTransport()
        ..replies['/api/v1/shifts/current/'] = {
          'shift': {
            'id': 'shift-1',
            'cashier_username': 'mary',
            'opening_float_cents': 100000,
            'opened_at': '2026-08-15T08:00:00+03:00',
          },
        };

      await tester.pumpWidget(
        host(ShiftScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.text('Expected'), findsNothing);
      expect(find.textContaining('should be'), findsNothing);
    });

    testWidgets('the expectation appears only after the count is committed',
        (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/shifts/current/'] = {
          'shift': {
            'id': 'shift-1',
            'cashier_username': 'mary',
            'opening_float_cents': 100000,
            'opened_at': '2026-08-15T08:00:00+03:00',
          },
        }
        ..replies['/api/v1/shifts/shift-1/close/'] = {
          'declared_closing_cents': 118000,
          'expected_closing_cents': 118000,
          'variance_cents': 0,
        };

      await tester.pumpWidget(
        host(ShiftScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('amount-field')), '118000');
      await tester.pump();
      await tester.tap(find.text('Close drawer'));
      await tester.pumpAndSettle();

      expect(find.text('The drawer balances'), findsOneWidget);
      expect(find.text('Expected'), findsOneWidget);
    });

    testWidgets('a variance says it was recorded, not that somebody is blamed',
        (tester) async {
      final transport = FakeTransport()
        ..replies['/api/v1/shifts/current/'] = {
          'shift': {
            'id': 'shift-1',
            'cashier_username': 'mary',
            'opening_float_cents': 100000,
            'opened_at': '2026-08-15T08:00:00+03:00',
          },
        }
        ..replies['/api/v1/shifts/shift-1/close/'] = {
          'declared_closing_cents': 113000,
          'expected_closing_cents': 118000,
          'variance_cents': -5000,
        };

      await tester.pumpWidget(
        host(ShiftScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('amount-field')), '113000');
      await tester.pump();
      await tester.tap(find.text('Close drawer'));
      await tester.pumpAndSettle();

      expect(find.text('The drawer is short'), findsOneWidget);
      expect(find.textContaining('recorded for a manager'), findsOneWidget);
      expect(find.textContaining('count stands'), findsOneWidget);
    });

    testWidgets('with no connection it explains why, and that selling is fine',
        (tester) async {
      final transport = FakeTransport()..throwThis = offline;

      await tester.pumpWidget(
        host(ShiftScreen(repository: ReportsRepository(transport))),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('No connection'), findsOneWidget);
      expect(find.textContaining('Selling carries on'), findsOneWidget);
    });
  });
}
