/// The kitchen ticket, and the guard against losing a waiter's order.
///
/// Two things this module gets wrong expensively if the client is careless:
/// a kitchen that cannot tell a reprint from a new ticket cooks the food twice,
/// and a waiter who taps Send into a silent failure loses six typed lines and
/// does not find out until a table asks where dinner is.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/core/api_client.dart';
import 'package:pos_till/data/printing/escpos.dart';
import 'package:pos_till/data/printing/kitchen_ticket.dart';
import 'package:pos_till/features/restaurant/order_unavailable.dart';

PrintableTicket ticket({
  int sequence = 1,
  bool isReprint = false,
  List<TicketLine>? lines,
  int covers = 0,
}) =>
    PrintableTicket(
      tableName: 'Table 4',
      sequence: sequence,
      isReprint: isReprint,
      covers: covers,
      waiter: 'Mary',
      printedAt: DateTime(2026, 8, 16, 19, 45),
      lines: lines ??
          const [
            TicketLine(
              name: 'Sirloin steak',
              quantityMilli: 1000,
              modifiers: ['Rare', 'No onions'],
            ),
          ],
    );

String printed(PrintableTicket value) =>
    plainText(renderKitchenTicket(value));

List<String> textLines(String output) => output.split('\n');

const offline = ApiException(
  status: 0,
  code: 'offline',
  message: 'No connection to the server.',
);

Widget host(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  group('what the kitchen is told', () {
    test('the table is the first thing on it', () {
      final lines = textLines(printed(ticket()));

      expect(lines.first.trim(), 'Table 4');
    });

    test('the ticket number is printed so a gap is visible', () {
      // Ticket 3 arriving after ticket 1 means something never came out of the
      // printer, and only the kitchen can notice that.
      expect(printed(ticket(sequence: 3)), contains('#3'));
    });

    test('items and quantities are there', () {
      final output = printed(ticket());

      expect(output, contains('1 Sirloin steak'));
    });

    test('modifiers ride under their line, not as separate dishes', () {
      final output = printed(ticket());

      expect(output, contains('   - Rare'));
      expect(output, contains('   - No onions'));
    });

    test('a free modifier is printed too', () {
      // The till has nothing to charge for "no onions"; the kitchen still needs
      // to be told.
      expect(printed(ticket()), contains('No onions'));
    });

    test('a note is marked so it is not skimmed past', () {
      final output = printed(
        ticket(
          lines: const [
            TicketLine(
              name: 'Sirloin steak',
              quantityMilli: 1000,
              note: 'Allergy - nuts',
            ),
          ],
        ),
      );

      expect(output, contains('! Allergy - nuts'));
    });

    test('a fractional quantity reads properly', () {
      final output = printed(
        ticket(
          lines: const [
            TicketLine(name: 'Chips', quantityMilli: 500),
          ],
        ),
      );

      expect(output, contains('0.5 Chips'));
    });

    test('covers are shown when known', () {
      expect(printed(ticket(covers: 4)), contains('4'));
    });
  });

  group('what the kitchen is deliberately not told', () {
    test('no prices', () {
      // Noise in a kitchen, and it costs paper and reading time on every
      // ticket.
      final output = printed(ticket());

      expect(output, isNot(contains('KES')));
      expect(output, isNot(contains('120.00')));
    });

    test('no tax and no total', () {
      final output = printed(ticket());

      expect(output.toUpperCase(), isNot(contains('VAT')));
      expect(output.toUpperCase(), isNot(contains('TOTAL')));
    });
  });

  group('a reprint says so, loudly', () {
    test('it is marked', () {
      // A kitchen that cannot tell a reprint from a new ticket cooks the food
      // twice, which is the expensive failure this design is arranged around.
      final output = printed(ticket(isReprint: true));

      expect(output, contains('REPRINT'));
      expect(output, contains('Check before cooking again'));
    });

    test('a first print carries no such mark', () {
      expect(printed(ticket()), isNot(contains('REPRINT')));
    });

    test('it keeps the ticket number it was', () {
      // A reprint of ticket two is ticket two, not "everything new now".
      expect(printed(ticket(sequence: 2, isReprint: true)), contains('#2'));
    });
  });

  group('fitting the paper', () {
    test('no line is wider than the printer', () {
      for (final line in textLines(printed(ticket()))) {
        expect(line.length, lessThanOrEqualTo(kThermalWidth), reason: 'line "$line"');
      }
    });

    test('a long dish name is truncated at double width', () {
      final output = textLines(
        printed(
          ticket(
            lines: const [
              TicketLine(
                name: 'Slow-braised beef short rib with roasted root vegetables',
                quantityMilli: 1000,
              ),
            ],
          ),
        ),
      );

      final dish = output.firstWhere((line) => line.startsWith('1 '));
      expect(dish.length, lessThanOrEqualTo(kDoubleWidth));
    });

    test('a long table name is truncated at double width too', () {
      final output = textLines(
        printed(
          PrintableTicket(
            tableName: 'Terrace table by the far window, seven',
            sequence: 1,
            lines: const [TicketLine(name: 'Soda', quantityMilli: 1000)],
          ),
        ),
      );

      expect(output.first.length, lessThanOrEqualTo(kDoubleWidth));
    });

    test('the paper is cut at the end', () {
      final bytes = renderKitchenTicket(ticket()).toList();

      expect(bytes.sublist(bytes.length - EscPos.cut.length), EscPos.cut);
    });
  });

  group('an order that cannot be sent', () {
    testWidgets('it says so rather than spinning', (tester) async {
      await tester.pumpWidget(
        host(OrderUnavailableBanner(error: offline, onRetry: () {}, lineCount: 6)),
      );

      expect(find.textContaining('cannot be sent right now'), findsOneWidget);
    });

    testWidgets('it says how much of the order is still there', (tester) async {
      // "Your order is safe" is only reassuring if it says how much of it.
      await tester.pumpWidget(
        host(OrderUnavailableBanner(error: offline, onRetry: () {}, lineCount: 6)),
      );

      expect(find.textContaining('6 lines you have typed'), findsOneWidget);
      expect(find.textContaining('nothing has been lost'), findsOneWidget);
    });

    testWidgets('it says nothing reached the kitchen', (tester) async {
      await tester.pumpWidget(
        host(OrderUnavailableBanner(error: offline, onRetry: () {}, lineCount: 2)),
      );

      expect(
        find.textContaining('Nothing has been sent to the kitchen'),
        findsOneWidget,
      );
    });

    testWidgets('it warns that orders are not saved on the tablet',
        (tester) async {
      // A waiter who assumes it will sync later, as sales do, will walk away
      // from the tablet.
      await tester.pumpWidget(
        host(OrderUnavailableBanner(error: offline, onRetry: () {}, lineCount: 3)),
      );

      expect(find.textContaining('not saved on the tablet'), findsOneWidget);
      expect(find.textContaining('Stay on this screen'), findsOneWidget);
    });

    testWidgets('it offers to try again', (tester) async {
      var tried = false;
      await tester.pumpWidget(
        host(
          OrderUnavailableBanner(
            error: offline,
            onRetry: () => tried = true,
            lineCount: 1,
          ),
        ),
      );

      await tester.tap(find.text('Try sending again'));
      await tester.pump();

      expect(tried, isTrue);
    });

    testWidgets('a refusal shows what the server said', (tester) async {
      // A 403 proves the server is reachable. Dressing it up as "no connection"
      // would send a waiter chasing a network fault that is not there.
      await tester.pumpWidget(
        host(
          OrderUnavailableBanner(
            error: const ApiException(
              status: 403,
              code: 'not_allowed',
              message: 'Only a manager can cancel that.',
            ),
            onRetry: () {},
            lineCount: 2,
          ),
        ),
      );

      expect(find.text('Only a manager can cancel that.'), findsOneWidget);
      expect(find.textContaining('not saved on the tablet'), findsNothing);
    });

    test('a refusal is not treated as a lost connection', () {
      expect(looksLikeNoConnection(offline), isTrue);
      expect(
        looksLikeNoConnection(
          const ApiException(status: 403, code: 'x', message: 'no'),
        ),
        isFalse,
      );
      expect(
        looksLikeNoConnection(
          const ApiException(status: 500, code: 'x', message: 'no'),
        ),
        isTrue,
      );
    });
  });

  group('the floor that cannot be loaded', () {
    testWidgets('an empty restaurant and a lost connection look different',
        (tester) async {
      await tester.pumpWidget(
        host(OrdersUnavailableView(error: offline, onRetry: () {})),
      );

      expect(find.textContaining('This is not an empty restaurant'), findsOneWidget);
    });

    testWidgets('it explains where orders live', (tester) async {
      await tester.pumpWidget(
        host(OrdersUnavailableView(error: offline, onRetry: () {})),
      );

      expect(find.textContaining('Orders live on the server'), findsOneWidget);
    });
  });
}
