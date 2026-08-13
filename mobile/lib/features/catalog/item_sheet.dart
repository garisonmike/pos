import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../data/models.dart';

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
              // Selling arrives in milestone 3. Saying so is better than an
              // absent button, which reads as something being broken.
              OutlinedButton(
                onPressed: null,
                child: const Text('Selling comes next'),
              ),
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
