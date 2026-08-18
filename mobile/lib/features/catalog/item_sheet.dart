import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../data/cart/cart_controller.dart';
import '../../data/cart/pricing.dart';
import '../../data/models.dart';
import '../sell/cart_screen.dart';
import '../sell/quantity_sheet.dart';

/// Everything known about one item, on a sheet rather than a page.
///
/// A sheet because looking something up at a counter is a glance, not a
/// journey: it slides over what the cashier was doing and dismisses with a
/// downward flick, so nobody loses their place in a list they had scrolled.
void showItemSheet(BuildContext context, Item item) {
  showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (context) => _ItemSheet(item: item),
  );
}

class _ItemSheet extends StatelessWidget {
  const _ItemSheet({required this.item});

  final Item item;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;

    return SafeArea(
      child: ConstrainedBox(
        // Never taller than most of the screen, so the sheet always reads as
        // something laid over the list rather than a new page.
        constraints: BoxConstraints(
          maxHeight: MediaQuery.of(context).size.height * 0.8,
        ),
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(item.name, style: text.headlineSmall),
              const SizedBox(height: 8),
              Text(
                item.priceDisplay,
                style: TextStyle(
                  fontSize: 30,
                  fontWeight: FontWeight.w700,
                  color: Theme.of(context).colorScheme.primary,
                ),
              ),
              if (item.isPriceVariable) ...[
                const SizedBox(height: 4),
                Text(
                  'Price is entered at the till',
                  style: TextStyle(fontSize: 15, color: TillTheme.muted),
                ),
              ],
              const SizedBox(height: 24),

              _Row(label: 'Code', value: item.sku),
              if (item.categoryName != null)
                _Row(label: 'Category', value: item.categoryName!),
              _Row(label: 'Sold by', value: _unitLabel(item.unit)),
              if (item.isService && item.durationMinutes != null)
                _Row(label: 'Takes', value: '${item.durationMinutes} minutes'),
              _Row(
                label: 'Available',
                value: item.isAvailable ? 'Yes' : 'Not right now',
                tone: item.isAvailable ? null : TillTheme.warning,
              ),

              if (item.barcodes.isNotEmpty) ...[
                const SizedBox(height: 16),
                Text('Barcodes', style: text.titleMedium),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final code in item.barcodes)
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 8,
                        ),
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: TillTheme.line),
                        ),
                        child: Text(
                          code,
                          style: const TextStyle(
                            fontSize: 15,
                            fontFeatures: [FontFeature.tabularFigures()],
                          ),
                        ),
                      ),
                  ],
                ),
              ],

              if (item.tracksStock) ...[
                const SizedBox(height: 16),
                Text('In stock', style: text.titleMedium),
                const SizedBox(height: 8),
                if (item.stock.isEmpty)
                  Text(
                    'Not stocked at any branch yet',
                    style: TextStyle(fontSize: 16, color: TillTheme.muted),
                  )
                else
                  for (final level in item.stock)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 6),
                      child: Row(
                        children: [
                          Text(
                            level.storeCode,
                            style: const TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const Spacer(),
                          if (level.isLow) ...[
                            Icon(
                              Icons.warning_amber_rounded,
                              size: 20,
                              color: TillTheme.warning,
                            ),
                            const SizedBox(width: 6),
                          ],
                          Text(
                            level.display,
                            style: TextStyle(
                              fontSize: 18,
                              fontWeight: FontWeight.w700,
                              color: level.isLow ? TillTheme.warning : null,
                            ),
                          ),
                        ],
                      ),
                    ),
              ],

              const SizedBox(height: 24),
              _AddToCartButton(item: item),
            ],
          ),
        ),
      ),
    );
  }

  static String _unitLabel(String unit) => switch (unit) {
    'KG' => 'Kilogram',
    'G' => 'Gram',
    'L' => 'Litre',
    'ML' => 'Millilitre',
    'M' => 'Metre',
    'HOUR' => 'Hour',
    _ => 'Each',
  };
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value, this.tone});

  final String label;
  final String value;
  final Color? tone;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(
        children: [
          SizedBox(
            width: 110,
            child: Text(
              label,
              style: TextStyle(fontSize: 16, color: TillTheme.muted),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: TextStyle(
                fontSize: 16,
                fontWeight: FontWeight.w600,
                color: tone,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// The button that finally connects the catalogue to the till.
///
/// Everything behind it - the cart, the tender screen, the receipt - was built
/// and tested in milestone 3, and none of it was reachable: this sheet still
/// carried a disabled "Selling comes next" placeholder from milestone 2, and
/// nothing anywhere else in the app navigated to [CartScreen]. The engine and
/// the ignition were finished separately and never wired together, which no
/// widget test could notice because each half passed on its own.
class _AddToCartButton extends ConsumerWidget {
  const _AddToCartButton({required this.item});

  final Item item;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // An unavailable item is refused here rather than at the till, so a
    // cashier finds out while looking at the item and not with a customer
    // waiting and a cart already half built.
    if (!item.isAvailable) {
      return const OutlinedButton(
        onPressed: null,
        child: Text('Not available'),
      );
    }

    return FilledButton.icon(
      onPressed: () => _add(context, ref),
      icon: const Icon(Icons.add_shopping_cart),
      label: const Text('Add to cart'),
    );
  }

  Future<void> _add(BuildContext context, WidgetRef ref) async {
    var unitPriceCents = item.price.cents;
    var quantityMilli = 1000;

    // A variable-price item has no price until somebody types one - an open
    // service, or produce priced at the counter. Asking first, because adding
    // it at zero and correcting later is how a sale goes out at zero.
    if (item.isPriceVariable) {
      final typed = await _askPrice(context);
      if (typed == null) return;
      unitPriceCents = typed;
    }

    // Anything not sold by the piece needs a real quantity: 0.75 kg of sugar
    // cannot be expressed by tapping "add" once. The sheet returns thousandths
    // so the fraction stays exact.
    if (item.unit != 'EACH') {
      if (!context.mounted) return;
      final measured = await showQuantitySheet(
        context,
        itemName: item.name,
        unit: item.unit,
        unitPriceCents: unitPriceCents,
      );
      if (measured == null) return;
      quantityMilli = measured;
    }

    ref
        .read(cartControllerProvider.notifier)
        .add(
          LineInput(
            itemId: item.id,
            name: item.name,
            sku: item.sku,
            unit: item.unit,
            unitPriceCents: unitPriceCents,
            quantityMilli: quantityMilli,
          ),
          // A price typed at the counter is never merged with another line, even
          // for the same item: two different prices for one thing are two
          // decisions somebody made, and folding them together silently discards
          // one of them.
          mergeable: !item.isPriceVariable,
        );

    if (!context.mounted) return;
    Navigator.of(context).pop();
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('${item.name} added'),
        duration: const Duration(seconds: 2),
        action: SnackBarAction(
          label: 'View cart',
          onPressed: () => Navigator.of(
            context,
          ).push(MaterialPageRoute<void>(builder: (_) => const CartScreen())),
        ),
      ),
    );
  }

  /// Ask for the price of a variable-price item, in shillings.
  Future<int?> _askPrice(BuildContext context) async {
    final controller = TextEditingController();
    final shillings = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Price for ${item.name}'),
        content: TextField(
          controller: controller,
          autofocus: true,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          decoration: const InputDecoration(
            prefixText: 'KES ',
            hintText: '0.00',
          ),
          onSubmitted: (value) => Navigator.pop(context, value),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text),
            child: const Text('Add'),
          ),
        ],
      ),
    );

    if (shillings == null) return null;
    final parsed = double.tryParse(shillings.trim());
    if (parsed == null || parsed <= 0) return null;
    // Rounded, not truncated, and only at this boundary: everything downstream
    // is integer cents.
    return (parsed * 100).round();
  }
}
