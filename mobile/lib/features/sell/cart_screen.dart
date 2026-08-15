/// The cart, and the pad that takes the money.
///
/// Laid out for one hand on a tablet propped at a counter: every control is at
/// least [TillTheme.minTapTarget] tall, the figures are large enough to read at
/// arm's length in daylight, and the primary action is a full-width bar at the
/// bottom where a thumb already is.
///
/// The total is shown at the **rounded** figure, because that is what the
/// cashier is about to ask for. Showing the exact total and then taking a
/// different amount is how a drawer ends the day a few shillings out with
/// nobody able to say why.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../data/cart/cart_controller.dart';
import '../../data/cart/pricing.dart';
import '../../data/models.dart';
import 'connectivity.dart';
import 'quantity_sheet.dart';

class CartScreen extends ConsumerWidget {
  const CartScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cart = ref.watch(cartControllerProvider);
    final totals = cart.totals;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Cart'),
        actions: [
          if (cart.isNotEmpty)
            TextButton(
              onPressed: () => _confirmClear(context, ref),
              child: const Text('Clear'),
            ),
        ],
      ),
      body: Column(
        children: [
          const OfflineBanner(),
          Expanded(
            child: cart.isEmpty
                ? const _EmptyCart()
                : ListView.separated(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    itemCount: totals.lines.length,
                    separatorBuilder: (_, __) => Divider(
                      height: 1,
                      color: TillTheme.line,
                    ),
                    itemBuilder: (context, index) => _CartRow(
                      line: totals.lines[index],
                      onQuantity: (value) => ref
                          .read(cartControllerProvider.notifier)
                          .setQuantity(index, value),
                      onRemove: () => ref
                          .read(cartControllerProvider.notifier)
                          .removeAt(index),
                    ),
                  ),
          ),
          if (cart.isNotEmpty) _Summary(totals: totals, cart: cart),
        ],
      ),
    );
  }

  Future<void> _confirmClear(BuildContext context, WidgetRef ref) async {
    // Asked rather than done, because clearing a cart a cashier has spent two
    // minutes building is not undoable and the button sits next to the ones
    // they use constantly.
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Clear the cart?'),
        content: const Text('Everything scanned so far will be removed.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Keep it'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Clear'),
          ),
        ],
      ),
    );
    if (confirmed ?? false) {
      ref.read(cartControllerProvider.notifier).clear();
    }
  }
}

class _EmptyCart extends StatelessWidget {
  const _EmptyCart();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.shopping_basket_outlined, size: 64, color: TillTheme.muted),
          const SizedBox(height: 16),
          Text(
            'Nothing scanned yet',
            style: TextStyle(fontSize: 18, color: TillTheme.muted),
          ),
        ],
      ),
    );
  }
}

class _CartRow extends StatelessWidget {
  const _CartRow({
    required this.line,
    required this.onQuantity,
    required this.onRemove,
  });

  final LineTotals line;
  final ValueChanged<int> onQuantity;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  line.line.name,
                  style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 2),
                Text(
                  // The unit belongs here. "2.5 × KES 180.00" tells a cashier
                  // nothing about whether that is kilos or bags, and the two
                  // are a very different amount of sugar.
                  '${_quantity(line.line.quantityMilli)}'
                  '${_unitSuffix(line.line.unit)} × '
                  '${Money(line.line.unitPriceCents).format()}'
                  '${_perUnit(line.line.unit)}',
                  style: TextStyle(color: TillTheme.muted, fontSize: 14),
                ),
                if (line.totalDiscountCents > 0)
                  Text(
                    'less ${Money(line.totalDiscountCents).format()}',
                    style: TextStyle(color: TillTheme.warning, fontSize: 13),
                  ),
              ],
            ),
          ),
          // Whole-unit steppers only where whole units are what is sold. A
          // measured item opens the keypad instead: stepping to 0.35 kg a
          // thousandth at a time is not a thing anybody would do.
          if (!isCountedEach(line.line.unit))
            Expanded(
              flex: 0,
              child: TextButton(
                onPressed: () => _typeQuantity(context),
                style: TextButton.styleFrom(
                  minimumSize: const Size(120, TillTheme.minTapTarget),
                ),
                child: Text(
                  '${_quantity(line.line.quantityMilli)}'
                  '${_unitSuffix(line.line.unit)}',
                  style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
              ),
            )
          else ...[
            _StepButton(
              icon: Icons.remove,
              onPressed: () => onQuantity(line.line.quantityMilli - 1000),
            ),
            SizedBox(
              width: 56,
              child: Text(
                _quantity(line.line.quantityMilli),
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
              ),
            ),
            _StepButton(
              icon: Icons.add,
              onPressed: () => onQuantity(line.line.quantityMilli + 1000),
            ),
          ],
          SizedBox(
            width: 96,
            child: Text(
              Money(line.grossCents).format(currency: ''),
              textAlign: TextAlign.right,
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700),
            ),
          ),
          IconButton(
            onPressed: onRemove,
            icon: const Icon(Icons.close),
            tooltip: 'Remove',
          ),
        ],
      ),
    );
  }

  Future<void> _typeQuantity(BuildContext context) async {
    final chosen = await showQuantitySheet(
      context,
      itemName: line.line.name,
      unit: line.line.unit,
      unitPriceCents: line.line.unitPriceCents,
      initialMilli: line.line.quantityMilli,
    );
    if (chosen != null) onQuantity(chosen);
  }

  static String _unitSuffix(String unit) {
    final label = unitLabel(unit);
    return label.isEmpty ? '' : ' $label';
  }

  static String _perUnit(String unit) {
    final label = unitLabel(unit);
    return label.isEmpty ? '' : ' / $label';
  }

  static String _quantity(int milli) {
    if (milli % 1000 == 0) return (milli ~/ 1000).toString();
    final text = (milli / 1000).toStringAsFixed(3).replaceAll(RegExp(r'0+$'), '');
    return text.endsWith('.') ? text.substring(0, text.length - 1) : text;
  }
}

class _StepButton extends StatelessWidget {
  const _StepButton({required this.icon, required this.onPressed});

  final IconData icon;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: TillTheme.minTapTarget,
      height: TillTheme.minTapTarget,
      child: IconButton(
        onPressed: () {
          // A short buzz, because a cashier is looking at the customer rather
          // than the screen and needs to know the tap registered.
          HapticFeedback.selectionClick();
          onPressed();
        },
        icon: Icon(icon),
      ),
    );
  }
}

class _Summary extends StatelessWidget {
  const _Summary({required this.totals, required this.cart});

  final CartTotals totals;
  final CartState cart;

  @override
  Widget build(BuildContext context) {
    final blocked = cart.needsAuthorization;

    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border(top: BorderSide(color: TillTheme.line)),
      ),
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      child: SafeArea(
        top: false,
        child: Column(
          children: [
            _row('Subtotal', totals.subtotalCents),
            if (totals.discountCents > 0)
              _row('Discount', -totals.discountCents, colour: TillTheme.warning),
            if (totals.taxCents > 0) _row('VAT', totals.taxCents),
            if (totals.roundingAdjustmentCents != 0)
              _row('Rounding', totals.roundingAdjustmentCents),
            const SizedBox(height: 6),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text(
                  'Total',
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700),
                ),
                Text(
                  // The rounded figure: what the cashier is about to ask for.
                  Money(totals.cashDueCents).format(),
                  style: const TextStyle(fontSize: 28, fontWeight: FontWeight.w800),
                ),
              ],
            ),
            if (blocked) ...[
              const SizedBox(height: 8),
              Row(
                children: [
                  Icon(Icons.lock_outline, size: 18, color: TillTheme.danger),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'A manager has to approve this discount before you can '
                      'take payment.',
                      style: TextStyle(color: TillTheme.danger, fontSize: 14),
                    ),
                  ),
                ],
              ),
            ],
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              height: TillTheme.primaryActionHeight,
              child: FilledButton(
                onPressed: blocked
                    ? null
                    : () => Navigator.of(context).pushNamed('/sell/pay'),
                child: Text(
                  'Take payment  ${Money(totals.cashDueCents).format()}',
                  style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _row(String label, int cents, {Color? colour}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label, style: TextStyle(color: colour ?? TillTheme.muted)),
            Text(
              Money(cents).format(currency: ''),
              style: TextStyle(
                color: colour ?? TillTheme.muted,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
      );
}
