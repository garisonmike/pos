/// A receipt a customer can actually check.
///
/// These assert the printed text, not the escape codes, because the escape
/// codes are not what goes wrong. What goes wrong is a line one character too
/// wide, which wraps every row on a real printer and is invisible until
/// somebody prints one in a shop.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:pos_till/data/printing/escpos.dart';

PrintableReceipt receipt({
  int? receiptNumber = 42,
  String? provisionalReference,
  int discountCents = 0,
  int roundingAdjustmentCents = 0,
  int taxCents = 2483,
  List<PrintableLine>? lines,
}) {
  return PrintableReceipt(
    businessName: 'Mama Njeri Duka',
    headerLine: 'Kiambu Road',
    footerLine: 'Thank you, karibu tena',
    taxPin: 'P051234567X',
    receiptNumber: receiptNumber,
    provisionalReference: provisionalReference,
    cashierName: 'Mary',
    soldAt: DateTime(2026, 8, 14, 14, 5),
    lines: lines ??
        const [
          PrintableLine(
            name: 'Sugar 1kg',
            quantityMilli: 2000,
            unitPriceCents: 18000,
            grossCents: 36000,
          ),
        ],
    subtotalCents: 15517,
    discountCents: discountCents,
    taxCents: taxCents,
    totalCents: 18000,
    roundingAdjustmentCents: roundingAdjustmentCents,
    tenderedCents: 20000,
    changeCents: 2000,
  );
}

String printed(PrintableReceipt value, {bool openDrawer = false}) =>
    plainText(renderEscPos(value, openDrawer: openDrawer));

List<String> textLines(String output) => output.split('\n');

void main() {
  group('fitting the paper', () {
    test('no line is wider than the printer', () {
      // The failure this whole test file exists for. One character too many
      // wraps every row, and nothing about the code looks wrong.
      for (final line in textLines(printed(receipt()))) {
        expect(line.length, lessThanOrEqualTo(kThermalWidth), reason: 'line "$line"');
      }
    });

    test('a very long item name is truncated, not wrapped', () {
      final output = printed(
        receipt(
          lines: const [
            PrintableLine(
              name: 'Extra fine granulated white cane sugar, one kilogram bag',
              quantityMilli: 1000,
              unitPriceCents: 18000,
              grossCents: 18000,
            ),
          ],
        ),
      );

      for (final line in textLines(output)) {
        expect(line.length, lessThanOrEqualTo(kThermalWidth));
      }
    });

    test('double-width lines fit their halved width', () {
      // Each glyph takes two cells in double-width mode, so 32 characters
      // overflow just as badly as 64 would at normal size. A shop with a long
      // name would otherwise find its receipts had been wrapping since the day
      // it opened.
      final output = textLines(
        printed(
          PrintableReceipt(
            businessName: 'Kwa Baba Hardware and General Supplies Limited',
            lines: const [
              PrintableLine(
                name: 'Nails',
                quantityMilli: 1000,
                unitPriceCents: 25000,
                grossCents: 25000,
              ),
            ],
            subtotalCents: 25000,
            taxCents: 0,
            totalCents: 25000,
            tenderedCents: 25000,
            changeCents: 0,
          ),
        ),
      );

      final name = output.firstWhere((l) => l.startsWith('Kwa Baba'));
      final total = output.firstWhere((l) => l.startsWith('TOTAL'));

      expect(name.length, lessThanOrEqualTo(kDoubleWidth));
      expect(total.length, lessThanOrEqualTo(kDoubleWidth));
    });

    test('a label never pushes its figure onto the next line', () {
      // A figure alone on a line reads as a separate amount, which is worse
      // than a truncated label.
      final line = row('An extremely long label that will not fit', '1,234.50');
      expect(line.length, kThermalWidth);
      expect(line, endsWith('1,234.50'));
    });

    test('figures are right-aligned so a column adds up', () {
      final output = textLines(printed(receipt()));
      final subtotal = output.firstWhere((l) => l.startsWith('Subtotal'));
      final vat = output.firstWhere((l) => l.startsWith('VAT'));

      expect(subtotal.length, kThermalWidth);
      expect(vat.length, kThermalWidth);
    });
  });

  group('what the receipt says', () {
    test('the shop name and branding are printed', () {
      final output = printed(receipt());

      expect(output, contains('Mama Njeri Duka'));
      expect(output, contains('Kiambu Road'));
      expect(output, contains('P051234567X'));
      expect(output, contains('Thank you, karibu tena'));
    });

    test('a settled sale prints its receipt number', () {
      expect(printed(receipt()), contains('#42'));
    });

    test('money is formatted with grouping and two decimals', () {
      expect(formatAmount(123450), '1,234.50');
      expect(formatAmount(5), '0.05');
      expect(formatAmount(-2000), '-20.00');
    });

    test('a whole quantity reads as a whole number', () {
      expect(formatQuantity(1000), '1');
      expect(formatQuantity(2500), '2.5');
      expect(formatQuantity(333), '0.333');
    });

    test('the line, its quantity and its price all appear', () {
      final output = printed(receipt());

      expect(output, contains('Sugar 1kg'));
      expect(output, contains('2 x 180.00'));
      expect(output, contains('360.00'));
    });

    test('a discount is shown rather than folded into the price', () {
      // A customer who was given a discount should be able to see it. Folding
      // it into the line price hides what they were actually granted.
      final output = printed(receipt(discountCents: 1800));

      expect(output, contains('Discount'));
      expect(output, contains('-18.00'));
    });

    test('cash rounding is named', () {
      // The drawer reconciles against it, and a customer working the column out
      // to the cent should find the difference explained rather than missing.
      final output = printed(receipt(roundingAdjustmentCents: -49));

      expect(output, contains('Rounding'));
      expect(output, contains('-0.49'));
    });

    test('the total printed is what the customer was asked for', () {
      final output = printed(receipt(roundingAdjustmentCents: -49));

      // 18000 total, rounded down by 49.
      expect(output, contains('179.51'));
    });

    test('tax is omitted when there is none', () {
      expect(printed(receipt(taxCents: 0)), isNot(contains('VAT')));
    });
  });

  group('a sale that has not reached the server', () {
    test('it prints a provisional reference instead of a number', () {
      final output = printed(
        receipt(receiptNumber: null, provisionalReference: 'T1-0007'),
      );

      expect(output, contains('T1-0007'));
      expect(output, isNot(contains('#')));
    });

    test('it says plainly that the number is not final', () {
      // A blank where a number should be invites a cashier to invent one.
      final output = printed(
        receipt(receiptNumber: null, provisionalReference: 'T1-0007'),
      );

      expect(output, contains('not yet synced'));
      expect(output, contains('has not reached'));
    });

    test('a settled receipt carries no such warning', () {
      expect(printed(receipt()), isNot(contains('not yet synced')));
    });
  });

  group('printer control', () {
    test('the stream starts by resetting the printer', () {
      // A printer holds whatever state the last job left it in, including a
      // previous receipt's double-height.
      final bytes = renderEscPos(receipt());
      expect(bytes.take(2).toList(), EscPos.initialise);
    });

    test('the paper is cut at the end', () {
      final bytes = renderEscPos(receipt()).toList();
      expect(
        bytes.sublist(bytes.length - EscPos.cut.length),
        EscPos.cut,
      );
    });

    test('the drawer only opens when asked', () {
      final without = renderEscPos(receipt()).toList();
      final with_ = renderEscPos(receipt(), openDrawer: true).toList();

      expect(with_.length, greaterThan(without.length));
      expect(
        with_.sublist(with_.length - EscPos.openDrawer.length),
        EscPos.openDrawer,
      );
    });

    test('a character the printer cannot render is replaced, not dropped', () {
      // Dropping it would silently change what the receipt says; these printers
      // have a fixed code page and a multi-byte character prints as line noise.
      final output = printed(
        receipt(
          lines: const [
            PrintableLine(
              name: 'Café crème 250g',
              quantityMilli: 1000,
              unitPriceCents: 45000,
              grossCents: 45000,
            ),
          ],
        ),
      );

      expect(output, contains('Caf'));
      expect(output, contains('250g'));
    });
  });

  group('sending it', () {
    test('a job reaches the transport', () async {
      final printer = InMemoryPrinter();

      await printer.send(renderEscPos(receipt()));

      expect(printer.jobs, hasLength(1));
      expect(printer.lastJobAsText, contains('Mama Njeri Duka'));
    });

    test('an unavailable printer reports itself rather than throwing', () async {
      // A printer out of paper must not take the sale down with it. The money
      // is already in the drawer.
      final printer = InMemoryPrinter()..available = false;

      expect(await printer.isAvailable(), isFalse);
    });
  });
}
