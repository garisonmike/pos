/// Turning a receipt into bytes a thermal printer understands.
///
/// ESC/POS is the command language every cheap Bluetooth till printer speaks.
/// It is a byte stream, not a document format: you send text, and you send
/// escape sequences that change how the text after them is printed.
///
/// **Byte building is kept separate from sending.** The bytes are pure, so the
/// layout can be tested without a printer in the room - which matters, because
/// a receipt that is one character too wide wraps every line and the fault is
/// invisible until someone prints one. [PrinterTransport] is the seam where the
/// Bluetooth connection goes.
///
/// The width is 32 characters, matching the server's `THERMAL_WIDTH`. A 58mm
/// printer at the usual font fits exactly that, and the server renders the same
/// receipt to the same width for the PDF - so the two cannot disagree about
/// what a customer was handed.
library;

import 'dart:convert';
import 'dart:typed_data';

/// Characters across a 58mm thermal printer at the usual font.
const int kThermalWidth = 32;

/// The ESC/POS commands this app uses.
///
/// Spelled out as named constants rather than magic byte lists, because
/// `[0x1B, 0x61, 0x01]` at a call site tells a reader nothing and the next
/// person to touch it will not know whether changing it is safe.
class EscPos {
  /// Reset to defaults. Sent first, because a printer holds whatever state the
  /// last job left it in - including a previous receipt's double-height.
  static const initialise = [0x1B, 0x40];

  static const alignLeft = [0x1B, 0x61, 0x00];
  static const alignCentre = [0x1B, 0x61, 0x01];

  static const boldOn = [0x1B, 0x45, 0x01];
  static const boldOff = [0x1B, 0x45, 0x00];

  /// Double width and height, for the total and the shop's name.
  static const doubleSize = [0x1D, 0x21, 0x11];
  static const normalSize = [0x1D, 0x21, 0x00];

  /// Cut the paper, leaving a small tail so the cut misses the last line.
  static const cut = [0x1D, 0x56, 0x42, 0x00];

  /// Open the cash drawer. Wired to the printer's kick-out port, which is how
  /// a drawer opens on a till that has no other connection to one.
  static const openDrawer = [0x1B, 0x70, 0x00, 0x19, 0xFA];
}

/// A receipt, as far as the printer is concerned.
class PrintableReceipt {
  const PrintableReceipt({
    required this.businessName,
    required this.lines,
    required this.subtotalCents,
    required this.taxCents,
    required this.totalCents,
    required this.tenderedCents,
    required this.changeCents,
    this.headerLine = '',
    this.footerLine = 'Thank you',
    this.taxPin = '',
    this.receiptNumber,
    this.provisionalReference,
    this.cashierName = '',
    this.discountCents = 0,
    this.roundingAdjustmentCents = 0,
    this.soldAt,
  });

  final String businessName;
  final String headerLine;
  final String footerLine;
  final String taxPin;

  /// Null until the sale has reached the server and been given a number.
  final int? receiptNumber;

  /// What an unsynced sale prints instead. A customer needs *something* to
  /// quote when they come back, and a blank space where a number should be
  /// invites a cashier to invent one.
  final String? provisionalReference;

  final String cashierName;
  final DateTime? soldAt;

  final List<PrintableLine> lines;
  final int subtotalCents;
  final int discountCents;
  final int taxCents;
  final int totalCents;
  final int roundingAdjustmentCents;
  final int tenderedCents;
  final int changeCents;

  /// What the customer was actually asked for, after cash rounding.
  int get dueCents => totalCents + roundingAdjustmentCents;

  /// Whether this is a receipt for a sale the server has not seen yet.
  bool get isProvisional => receiptNumber == null;
}

class PrintableLine {
  const PrintableLine({
    required this.name,
    required this.quantityMilli,
    required this.unitPriceCents,
    required this.grossCents,
    this.discountCents = 0,
  });

  final String name;
  final int quantityMilli;
  final int unitPriceCents;
  final int grossCents;
  final int discountCents;
}

/// Format cents the way a receipt does: grouped, two decimals, no currency.
String formatAmount(int cents) {
  final negative = cents < 0;
  final absolute = cents.abs();
  final whole = absolute ~/ 100;
  final fraction = (absolute % 100).toString().padLeft(2, '0');
  final grouped = whole.toString().replaceAllMapped(
        RegExp(r'(\d{1,3})(?=(\d{3})+$)'),
        (match) => '${match[1]},',
      );
  return '${negative ? '-' : ''}$grouped.$fraction';
}

/// Trim trailing zeros, so 1.000 reads as 1 and 2.500 as 2.5.
String formatQuantity(int quantityMilli) {
  if (quantityMilli % 1000 == 0) return (quantityMilli ~/ 1000).toString();
  var text = (quantityMilli / 1000).toStringAsFixed(3);
  text = text.replaceAll(RegExp(r'0+$'), '');
  return text.endsWith('.') ? text.substring(0, text.length - 1) : text;
}

/// One row with the label left and the figure right.
///
/// Amounts are right-aligned because that is how a person adds up a column, and
/// a receipt whose figures do not line up is one a customer cannot check. The
/// label is truncated rather than wrapped when it would collide, so the figure
/// is never pushed onto its own line where it reads as a separate amount.
String row(String left, String right, {int width = kThermalWidth}) {
  final space = width - right.length;
  var label = left;
  if (label.length >= space) {
    label = label.substring(0, space - 1 < 0 ? 0 : space - 1);
  }
  return label.padRight(width - right.length) + right;
}

String centred(String text, {int width = kThermalWidth}) {
  if (text.length >= width) return text.substring(0, width);
  final left = (width - text.length) ~/ 2;
  return ' ' * left + text;
}

String rule({int width = kThermalWidth, String character = '-'}) =>
    character * width;

/// Cut a string to what the paper holds.
///
/// Truncated rather than wrapped. A wrapped item name pushes the whole receipt
/// down and, worse, makes the second half look like a separate line item that
/// the customer was charged for.
String fit(String text, {int width = kThermalWidth}) =>
    text.length <= width ? text : text.substring(0, width);

/// Characters that fit when the printer is in double-width mode.
///
/// Half, because each glyph takes two cells. Forgetting this is how a shop with
/// a long name discovers its receipts have been wrapping since the day it
/// opened.
const int kDoubleWidth = kThermalWidth ~/ 2;

/// Build the byte stream for one receipt.
///
/// Latin-1 rather than UTF-8: these printers have a fixed code page and a
/// multi-byte character comes out as two pieces of line noise. Anything outside
/// it is replaced rather than dropped, so a name with an accent prints as a
/// recognisable approximation instead of silently losing a letter and changing
/// what the receipt says.
Uint8List renderEscPos(PrintableReceipt receipt, {bool openDrawer = false}) {
  final bytes = BytesBuilder();

  void raw(List<int> command) => bytes.add(command);
  void text(String value) => bytes.add(_encode('$value\n'));

  raw(EscPos.initialise);

  raw(EscPos.alignCentre);
  raw(EscPos.doubleSize);
  raw(EscPos.boldOn);
  // Double width, so only half as many characters fit.
  text(fit(receipt.businessName, width: kDoubleWidth));
  raw(EscPos.boldOff);
  raw(EscPos.normalSize);

  if (receipt.headerLine.isNotEmpty) text(fit(receipt.headerLine));
  if (receipt.taxPin.isNotEmpty) text(fit('PIN: ${receipt.taxPin}'));

  raw(EscPos.alignLeft);
  text(rule());

  if (receipt.receiptNumber != null) {
    text(row('Receipt', '#${receipt.receiptNumber}'));
  } else if (receipt.provisionalReference != null) {
    // Marked plainly, because a customer holding this needs to know the number
    // will change - and a cashier needs to know it is not final either.
    raw(EscPos.boldOn);
    text(row('Ref (not yet synced)', receipt.provisionalReference!));
    raw(EscPos.boldOff);
  }

  if (receipt.soldAt != null) {
    text(row('Date', _stamp(receipt.soldAt!)));
  }
  if (receipt.cashierName.isNotEmpty) {
    text(row('Served by', receipt.cashierName));
  }

  text(rule());

  for (final line in receipt.lines) {
    text(fit(line.name));
    final quantity = formatQuantity(line.quantityMilli);
    final unit = formatAmount(line.unitPriceCents);
    text(row('  $quantity x $unit', formatAmount(line.grossCents)));
    if (line.discountCents > 0) {
      text(row('  Discount', '-${formatAmount(line.discountCents)}'));
    }
  }

  text(rule());
  text(row('Subtotal', formatAmount(receipt.subtotalCents)));
  if (receipt.discountCents > 0) {
    text(row('Discount', '-${formatAmount(receipt.discountCents)}'));
  }
  if (receipt.taxCents > 0) {
    text(row('VAT', formatAmount(receipt.taxCents)));
  }
  if (receipt.roundingAdjustmentCents != 0) {
    // Printed because the drawer has to reconcile against it, and a customer
    // who works the column out to the cent should find the difference named.
    text(row('Rounding', formatAmount(receipt.roundingAdjustmentCents)));
  }

  raw(EscPos.doubleSize);
  raw(EscPos.boldOn);
  text(row('TOTAL', formatAmount(receipt.dueCents), width: kDoubleWidth));
  raw(EscPos.boldOff);
  raw(EscPos.normalSize);

  text(row('Cash', formatAmount(receipt.tenderedCents)));
  text(row('Change', formatAmount(receipt.changeCents)));

  text(rule());
  raw(EscPos.alignCentre);
  if (receipt.footerLine.isNotEmpty) text(fit(receipt.footerLine));
  if (receipt.isProvisional) {
    text('This sale has not reached');
    text('the server yet.');
  }

  // Feed past the tear bar before cutting, or the cut lands mid-text.
  text('');
  text('');
  raw(EscPos.cut);

  if (openDrawer) raw(EscPos.openDrawer);

  return bytes.toBytes();
}

Uint8List _encode(String value) => Uint8List.fromList(latin1.encode(
      String.fromCharCodes(
        value.runes.map((rune) => rune <= 0xFF ? rune : 0x3F),
      ),
    ));

String _stamp(DateTime at) {
  String two(int value) => value.toString().padLeft(2, '0');
  return '${at.year}-${two(at.month)}-${two(at.day)} '
      '${two(at.hour)}:${two(at.minute)}';
}

/// Read back what a receipt would actually say on paper.
///
/// Needed for two real jobs, not only for tests: showing a cashier a preview
/// before committing paper to it, and reprinting a receipt as plain text when
/// no printer is paired.
///
/// It has to understand the command sequences rather than merely dropping
/// control bytes, because an ESC/POS command carries *printable* bytes in the
/// middle of it - `ESC a 0` is `0x1B 0x61 0x00`, and stripping only the control
/// characters leaves a stray `a` glued to the front of the next line. That is
/// how a width check ends up passing on text nobody would ever print.
String plainText(Uint8List bytes) {
  final out = StringBuffer();
  var index = 0;

  while (index < bytes.length) {
    final byte = bytes[index];

    if (byte == 0x1B) {
      // ESC. The command byte decides how many operands follow.
      final command = index + 1 < bytes.length ? bytes[index + 1] : 0;
      index += switch (command) {
        0x40 => 2, // initialise
        0x61 || 0x45 => 3, // align, bold
        0x70 => 5, // open drawer
        _ => 2,
      };
      continue;
    }
    if (byte == 0x1D) {
      // GS.
      final command = index + 1 < bytes.length ? bytes[index + 1] : 0;
      index += switch (command) {
        0x21 => 3, // character size
        0x56 => 4, // cut
        _ => 2,
      };
      continue;
    }

    out.writeCharCode(byte);
    index++;
  }

  return out.toString();
}

/// The lines a person would read off the paper.
List<String> plainTextLines(Uint8List bytes) {
  final text = plainText(bytes);
  final lines = text.split('\n');
  // A trailing newline yields one empty element that was never a printed line.
  if (lines.isNotEmpty && lines.last.isEmpty) lines.removeLast();
  return lines;
}

/// Where the bytes actually go.
///
/// An interface because the transport is the part that cannot be tested here -
/// Bluetooth pairing, a printer that is out of paper, a socket that accepts the
/// write and drops it. Keeping it separate means the layout above is provable
/// and only the connection needs a device to exercise.
abstract class PrinterTransport {
  Future<bool> isAvailable();
  Future<void> send(Uint8List bytes);
}

/// A transport that keeps what it was given, for tests and for a till with no
/// printer paired yet.
class InMemoryPrinter implements PrinterTransport {
  final List<Uint8List> jobs = [];
  bool available = true;

  @override
  Future<bool> isAvailable() async => available;

  @override
  Future<void> send(Uint8List bytes) async => jobs.add(bytes);

  String get lastJobAsText => jobs.isEmpty ? '' : plainText(jobs.last);
}
