/// Typing a weight or a measure at the counter.
///
/// A cashier scooping sugar into a bag reads a figure off a scale and types it.
/// Stepping by whole kilos, which is all the cart row offers, cannot express
/// that - and 0.35 kg is an ordinary purchase, not an edge case.
///
/// **Thousandths, entered as whole digits.** The keypad builds an integer the
/// same way the tender pad builds cents: tapping 3, 5, 0 gives 0.350. Nothing
/// here parses a decimal string, because a double would eventually price a bag
/// of sugar a cent away from what the server charges - and the server's figure
/// is the one on the receipt.
///
/// Only shown for measured items. An item sold `EACH` keeps the plus and minus
/// buttons, which are faster for the thing they are for.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../core/theme.dart';
import '../../data/models.dart';

/// How a unit reads on screen.
///
/// The server's codes are terse because they travel; a cashier should see
/// "kg", not "KG".
String unitLabel(String unit) => switch (unit) {
      'KG' => 'kg',
      'G' => 'g',
      'L' => 'L',
      'ML' => 'ml',
      'M' => 'm',
      'HOUR' => 'hr',
      _ => '',
    };

/// Whether an item is counted in whole pieces.
bool isCountedEach(String unit) => unit == 'EACH' || unitLabel(unit).isEmpty;

/// Quantities a shop reaches for constantly, in thousandths.
///
/// Weight-shaped, because these are the ones a duka actually scoops: a quarter,
/// a half, three quarters, one, two. A cashier hitting one of these should not
/// have to type at all.
const List<int> kCommonQuantitiesMilli = [250, 500, 750, 1000, 2000, 5000];

/// The sheet itself. Returns the chosen quantity in thousandths, or null.
Future<int?> showQuantitySheet(
  BuildContext context, {
  required String itemName,
  required String unit,
  required int unitPriceCents,
  int initialMilli = 1000,
}) {
  return showModalBottomSheet<int>(
    context: context,
    isScrollControlled: true,
    builder: (context) => QuantitySheet(
      itemName: itemName,
      unit: unit,
      unitPriceCents: unitPriceCents,
      initialMilli: initialMilli,
    ),
  );
}

class QuantitySheet extends StatefulWidget {
  const QuantitySheet({
    super.key,
    required this.itemName,
    required this.unit,
    required this.unitPriceCents,
    this.initialMilli = 1000,
  });

  final String itemName;
  final String unit;
  final int unitPriceCents;
  final int initialMilli;

  @override
  State<QuantitySheet> createState() => _QuantitySheetState();
}

class _QuantitySheetState extends State<QuantitySheet> {
  /// Thousandths, built up digit by digit. Never parsed from a decimal string.
  int _milli = 0;

  /// Whether anything has been typed yet. Until it has, the sheet shows the
  /// quantity it opened with rather than a zero, so a cashier adjusting an
  /// existing line sees what is already there.
  bool _typed = false;

  int get _quantity => _typed ? _milli : widget.initialMilli;

  /// What this comes to, priced the same way the server will.
  ///
  /// Rounded half-up at the line, exactly as `LineInput.grossBeforeDiscount`
  /// does, so the figure a cashier reads here is the one that ends up on the
  /// receipt rather than one a cent away from it.
  int get _grossCents {
    final exact = widget.unitPriceCents * _quantity;
    return (2 * exact + 1000) ~/ 2000;
  }

  @override
  Widget build(BuildContext context) {
    final label = unitLabel(widget.unit);

    return SafeArea(
      child: Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.of(context).viewInsets.bottom,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 20, 20, 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    widget.itemName,
                    style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
                  ),
                  Text(
                    '${Money(widget.unitPriceCents).format()} per $label',
                    style: TextStyle(fontSize: 15, color: TillTheme.muted),
                  ),
                ],
              ),
            ),

            // The two figures a cashier is reconciling: what the scale says,
            // and what it comes to.
            Container(
              width: double.infinity,
              margin: const EdgeInsets.symmetric(horizontal: 20),
              padding: const EdgeInsets.symmetric(vertical: 18),
              decoration: BoxDecoration(
                color: const Color(0xFFF3F4F5),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Column(
                children: [
                  Text(
                    '${formatQuantity(_quantity)} $label',
                    style: const TextStyle(fontSize: 38, fontWeight: FontWeight.w800),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    Money(_grossCents).format(),
                    style: TextStyle(fontSize: 20, color: TillTheme.muted),
                  ),
                ],
              ),
            ),

            const SizedBox(height: 12),
            SizedBox(
              height: TillTheme.minTapTarget,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 16),
                children: [
                  for (final quantity in kCommonQuantitiesMilli)
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 4),
                      child: OutlinedButton(
                        onPressed: () {
                          HapticFeedback.selectionClick();
                          setState(() {
                            _typed = true;
                            _milli = quantity;
                          });
                        },
                        child: Text('${formatQuantity(quantity)} $label'),
                      ),
                    ),
                ],
              ),
            ),

            _Keypad(
              onDigit: (digit) => setState(() {
                _typed = true;
                // Capped so a stuck finger cannot produce a figure that
                // overflows the display and hides what is really there.
                final next = _milli * 10 + digit;
                if (next <= 9999999) _milli = next;
              }),
              onBackspace: () => setState(() {
                _typed = true;
                _milli ~/= 10;
              }),
              onClear: () => setState(() {
                _typed = true;
                _milli = 0;
              }),
            ),

            Padding(
              padding: const EdgeInsets.all(16),
              child: SizedBox(
                width: double.infinity,
                height: TillTheme.primaryActionHeight,
                child: FilledButton(
                  // A zero quantity is not a sale. Refused here rather than
                  // sent, so a cashier fixes it while the customer is standing
                  // there rather than reading a server error afterwards.
                  onPressed: _quantity <= 0
                      ? null
                      : () => Navigator.of(context).pop(_quantity),
                  child: Text(
                    _quantity <= 0
                        ? 'Enter a quantity'
                        : 'Add ${formatQuantity(_quantity)} $label',
                    style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w700),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Thousandths as a person writes them: 1000 is `1`, 2500 is `2.5`, 333 is
/// `0.333`.
String formatQuantity(int milli) {
  if (milli % 1000 == 0) return (milli ~/ 1000).toString();
  var text = (milli / 1000).toStringAsFixed(3);
  text = text.replaceAll(RegExp(r'0+$'), '');
  return text.endsWith('.') ? text.substring(0, text.length - 1) : text;
}

class _Keypad extends StatelessWidget {
  const _Keypad({
    required this.onDigit,
    required this.onBackspace,
    required this.onClear,
  });

  final ValueChanged<int> onDigit;
  final VoidCallback onBackspace;
  final VoidCallback onClear;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Column(
        children: [
          for (final row in const [
            [1, 2, 3],
            [4, 5, 6],
            [7, 8, 9],
          ])
            Row(
              children: [
                for (final digit in row)
                  Expanded(child: _Key(label: '$digit', onPressed: () => onDigit(digit))),
              ],
            ),
          Row(
            children: [
              Expanded(child: _Key(label: 'C', onPressed: onClear)),
              Expanded(child: _Key(label: '0', onPressed: () => onDigit(0))),
              Expanded(
                child: _Key(
                  label: '⌫',
                  semantic: 'Backspace',
                  onPressed: onBackspace,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _Key extends StatelessWidget {
  const _Key({required this.label, required this.onPressed, this.semantic});

  final String label;
  final VoidCallback onPressed;
  final String? semantic;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: semantic ?? label,
      button: true,
      child: Padding(
        padding: const EdgeInsets.all(4),
        child: SizedBox(
          height: TillTheme.minTapTarget,
          child: OutlinedButton(
            onPressed: () {
              HapticFeedback.selectionClick();
              onPressed();
            },
            child: Text(
              label,
              style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
            ),
          ),
        ),
      ),
    );
  }
}
