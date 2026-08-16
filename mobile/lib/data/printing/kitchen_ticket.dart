/// What the kitchen gets, as bytes.
///
/// **Not a receipt, and deliberately not shaped like one.** A kitchen needs
/// items, quantities, modifiers and a table number, large enough to read across
/// a hot room at a glance. Prices, tax and shop branding are noise there, and
/// printing them costs paper and reading time on every ticket.
///
/// Shares the byte layer with the receipt renderer, so there is one place that
/// knows what ESC/POS looks like.
library;

import 'dart:typed_data';

import 'escpos.dart';

/// One line as the kitchen reads it.
class TicketLine {
  const TicketLine({
    required this.name,
    required this.quantityMilli,
    this.modifiers = const [],
    this.note = '',
  });

  final String name;
  final int quantityMilli;

  /// Free and priced alike. The kitchen needs "no onions" even though the till
  /// has nothing to charge for it.
  final List<String> modifiers;

  /// Anything typed at the table. Allergies arrive here.
  final String note;
}

/// One ticket, as the server issued it.
class PrintableTicket {
  const PrintableTicket({
    required this.tableName,
    required this.sequence,
    required this.lines,
    this.printedAt,
    this.covers = 0,
    this.isReprint = false,
    this.waiter = '',
  });

  final String tableName;

  /// Numbered per order, printed so a kitchen can spot a gap. Ticket 3 arriving
  /// after ticket 1 means something never came out of the printer.
  final int sequence;

  final List<TicketLine> lines;
  final DateTime? printedAt;
  final int covers;

  /// Marked loudly. A kitchen that cannot tell a reprint from a new ticket
  /// cooks the food twice, which is the expensive failure this whole design
  /// is arranged around.
  final bool isReprint;

  final String waiter;
}

/// Build the byte stream for one kitchen ticket.
Uint8List renderKitchenTicket(PrintableTicket ticket) {
  final bytes = BytesBuilder();

  void raw(List<int> command) => bytes.add(command);
  void text(String value) => bytes.add(_encode('$value\n'));

  raw(EscPos.initialise);

  // The table, as big as the printer can make it. It is the only thing that
  // matters if the ticket is read from two metres away.
  raw(EscPos.alignCentre);
  raw(EscPos.doubleSize);
  raw(EscPos.boldOn);
  text(fit(ticket.tableName, width: kDoubleWidth));
  raw(EscPos.boldOff);
  raw(EscPos.normalSize);

  if (ticket.isReprint) {
    raw(EscPos.boldOn);
    text('*** REPRINT ***');
    text('Check before cooking again');
    raw(EscPos.boldOff);
  }

  raw(EscPos.alignLeft);
  text(rule());
  text(row('Ticket', '#${ticket.sequence}'));
  if (ticket.covers > 0) text(row('Covers', '${ticket.covers}'));
  if (ticket.waiter.isNotEmpty) text(row('Taken by', fit(ticket.waiter, width: 20)));
  if (ticket.printedAt != null) {
    text(row('Time', _clock(ticket.printedAt!)));
  }
  text(rule());

  for (final line in ticket.lines) {
    // Quantity and item on one line, at double height, because this is what a
    // cook actually reads.
    raw(EscPos.doubleSize);
    raw(EscPos.boldOn);
    text(fit('${formatQuantity(line.quantityMilli)} ${line.name}',
        width: kDoubleWidth));
    raw(EscPos.boldOff);
    raw(EscPos.normalSize);

    for (final modifier in line.modifiers) {
      // Indented and normal size: a qualification of the line above, not a
      // separate dish.
      text(fit('   - $modifier'));
    }
    if (line.note.isNotEmpty) {
      raw(EscPos.boldOn);
      text(fit('   ! ${line.note}'));
      raw(EscPos.boldOff);
    }
  }

  text(rule());
  // Fed past the tear bar before cutting, or the cut lands mid-text.
  text('');
  text('');
  raw(EscPos.cut);

  return bytes.toBytes();
}

Uint8List _encode(String value) => Uint8List.fromList(
      value.runes.map((rune) => rune <= 0xFF ? rune : 0x3F).toList(),
    );

String _clock(DateTime at) {
  String two(int value) => value.toString().padLeft(2, '0');
  return '${two(at.hour)}:${two(at.minute)}';
}
