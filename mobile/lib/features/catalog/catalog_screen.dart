import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../data/models.dart';
import '../../data/cart/cart_controller.dart';
import '../../providers.dart';
import '../sell/cart_screen.dart';
import 'item_sheet.dart';

/// Browsing what the shop sells.
///
/// Read-only for now: this is the screen that proves a client's catalogue is
/// really in the system, and the one selling gets built on top of in milestone
/// 3.
///
/// Searching and browsing are one screen rather than two, because to the person
/// holding the till they are the same activity - find the thing. Typing filters;
/// clearing the box returns to the category view.
class CatalogScreen extends ConsumerWidget {
  const CatalogScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final till = ref.watch(tillStateProvider).value;
    final items = ref.watch(visibleItemsProvider);

    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              till?.tenantName?.isNotEmpty == true
                  ? till!.tenantName!
                  : 'Catalogue',
              overflow: TextOverflow.ellipsis,
            ),
            if (till?.session != null)
              Text(
                '${till!.session!.shortName} · ${till.session!.roleLabel}',
                style: const TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w400,
                  color: Colors.white70,
                ),
              ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Scan a barcode',
            iconSize: 28,
            icon: const Icon(Icons.qr_code_scanner),
            onPressed: () => _promptForBarcode(context, ref),
          ),
          const _CartButton(),
          IconButton(
            tooltip: 'Sign out',
            iconSize: 28,
            icon: const Icon(Icons.logout),
            onPressed: () => ref.read(tillStateProvider.notifier).signOut(),
          ),
        ],
      ),
      body: Column(
        children: [
          const _SearchBar(),
          const _CategoryStrip(),
          Expanded(
            child: items.when(
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (error, _) => _ErrorState(
                error: error,
                onRetry: () => ref.invalidate(visibleItemsProvider),
              ),
              data: (rows) => rows.isEmpty
                  ? const _EmptyState()
                  : RefreshIndicator(
                      onRefresh: () async =>
                          ref.invalidate(visibleItemsProvider),
                      child: ListView.builder(
                        // Physics forced so pull-to-refresh works even when the
                        // list is short enough not to scroll on its own.
                        physics: const AlwaysScrollableScrollPhysics(),
                        padding: const EdgeInsets.only(bottom: 24),
                        itemCount: rows.length,
                        itemBuilder: (context, index) =>
                            _ItemRow(item: rows[index]),
                      ),
                    ),
            ),
          ),
        ],
      ),
    );
  }

  /// Stand-in for a camera scan.
  ///
  /// Most Kenyan counters use a USB or Bluetooth scanner that behaves as a
  /// keyboard, so typed entry is the realistic path and a camera is a
  /// convenience on top. Milestone 3 adds the camera; this already exercises
  /// the lookup that matters.
  Future<void> _promptForBarcode(BuildContext context, WidgetRef ref) async {
    final controller = TextEditingController();

    final code = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Scan or type a barcode'),
        content: TextField(
          controller: controller,
          autofocus: true,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(labelText: 'Barcode'),
          onSubmitted: (value) => Navigator.pop(context, value),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text),
            style: FilledButton.styleFrom(
              minimumSize: const Size(120, TillTheme.minTapTarget),
            ),
            child: const Text('Look up'),
          ),
        ],
      ),
    );

    if (code == null || code.trim().isEmpty || !context.mounted) return;

    final messenger = ScaffoldMessenger.of(context);
    try {
      final item = await ref
          .read(catalogRepositoryProvider)
          .lookupBarcode(code.trim());
      if (!context.mounted) return;

      if (item == null) {
        messenger.showSnackBar(
          SnackBar(
            backgroundColor: TillTheme.warning,
            content: Text('No item has the barcode ${code.trim()}'),
          ),
        );
        return;
      }
      showItemSheet(context, item);
    } on ApiException catch (error) {
      messenger.showSnackBar(SnackBar(content: Text(error.message)));
    }
  }
}

class _SearchBar extends ConsumerStatefulWidget {
  const _SearchBar();

  @override
  ConsumerState<_SearchBar> createState() => _SearchBarState();
}

class _SearchBarState extends ConsumerState<_SearchBar> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final query = ref.watch(searchQueryProvider);

    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 8),
      child: TextField(
        controller: _controller,
        textInputAction: TextInputAction.search,
        decoration: InputDecoration(
          hintText: 'Search name, code or barcode',
          prefixIcon: const Icon(Icons.search, size: 26),
          suffixIcon: query.isEmpty
              ? null
              : IconButton(
                  iconSize: 26,
                  icon: const Icon(Icons.clear),
                  onPressed: () {
                    _controller.clear();
                    ref.read(searchQueryProvider.notifier).state = '';
                  },
                ),
        ),
        onChanged: (value) =>
            ref.read(searchQueryProvider.notifier).state = value,
      ),
    );
  }
}

class _CategoryStrip extends ConsumerWidget {
  const _CategoryStrip();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final categories = ref.watch(categoriesProvider);
    final selected = ref.watch(selectedCategoryProvider);
    final searching = ref.watch(searchQueryProvider).trim().length >= 2;

    // Categories are irrelevant while searching, and leaving them visible
    // invites the reasonable-but-wrong assumption that they narrow the results.
    if (searching) return const SizedBox.shrink();

    return categories.maybeWhen(
      data: (rows) => rows.isEmpty
          ? const SizedBox.shrink()
          : SizedBox(
              height: 56,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: [
                  _CategoryChip(
                    label: 'All',
                    selected: selected == null,
                    onTap: () =>
                        ref.read(selectedCategoryProvider.notifier).state =
                            null,
                  ),
                  for (final category in rows)
                    _CategoryChip(
                      label: '${category.name} (${category.itemCount})',
                      selected: selected == category.id,
                      onTap: () =>
                          ref.read(selectedCategoryProvider.notifier).state =
                              category.id,
                    ),
                ],
              ),
            ),
      orElse: () => const SizedBox.shrink(),
    );
  }
}

class _CategoryChip extends StatelessWidget {
  const _CategoryChip({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final primary = Theme.of(context).colorScheme.primary;

    return Padding(
      padding: const EdgeInsets.only(right: 8, top: 4, bottom: 4),
      child: Material(
        color: selected ? primary : Colors.white,
        borderRadius: BorderRadius.circular(24),
        child: InkWell(
          borderRadius: BorderRadius.circular(24),
          onTap: onTap,
          child: Container(
            constraints: const BoxConstraints(minWidth: 64),
            padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(24),
              border: Border.all(
                color: selected ? primary : TillTheme.line,
                width: 1.5,
              ),
            ),
            alignment: Alignment.center,
            child: Text(
              label,
              style: TextStyle(
                fontSize: 16,
                fontWeight: FontWeight.w600,
                color: selected ? Colors.white : TillTheme.muted,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _ItemRow extends StatelessWidget {
  const _ItemRow({required this.item});

  final Item item;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => showItemSheet(context, item),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      item.name,
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        Text(
                          item.sku,
                          style: TextStyle(
                            fontSize: 14,
                            color: TillTheme.muted,
                          ),
                        ),
                        if (item.isService) ...[
                          const SizedBox(width: 8),
                          const _Tag(label: 'Service'),
                        ],
                        if (!item.isAvailable) ...[
                          const SizedBox(width: 8),
                          _Tag(label: 'Unavailable', color: TillTheme.warning),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Text(
                    item.priceDisplay,
                    style: const TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 4),
                  _StockLabel(item: item),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The quantity, or nothing at all.
///
/// An untracked item shows no stock line rather than a zero: a haircut with "0"
/// beside it reads as sold out, and a cashier would hesitate over something
/// they should just sell.
class _StockLabel extends StatelessWidget {
  const _StockLabel({required this.item});

  final Item item;

  @override
  Widget build(BuildContext context) {
    final summary = item.stockSummary;
    if (summary == null) return const SizedBox.shrink();

    final low = item.isLowOnStock;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (low) ...[
          Icon(Icons.warning_amber_rounded, size: 18, color: TillTheme.warning),
          const SizedBox(width: 4),
        ],
        Text(
          '$summary in stock',
          style: TextStyle(
            fontSize: 14,
            fontWeight: low ? FontWeight.w700 : FontWeight.w400,
            color: low ? TillTheme.warning : TillTheme.muted,
          ),
        ),
      ],
    );
  }
}

class _Tag extends StatelessWidget {
  const _Tag({required this.label, this.color});

  final String label;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final tone = color ?? TillTheme.muted;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: tone),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.w700,
          color: tone,
        ),
      ),
    );
  }
}

class _EmptyState extends ConsumerWidget {
  const _EmptyState();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final searching = ref.watch(searchQueryProvider).trim().length >= 2;

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              searching ? Icons.search_off : Icons.inventory_2_outlined,
              size: 56,
              color: TillTheme.muted,
            ),
            const SizedBox(height: 16),
            Text(
              searching ? 'Nothing matches that' : 'No items here yet',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 8),
            Text(
              searching
                  ? 'Try part of the name, the code, or scan the barcode.'
                  : 'Once items are added they will appear here.',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 16, color: TillTheme.muted),
            ),
          ],
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.error, required this.onRetry});

  final Object error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final api = error is ApiException ? error as ApiException : null;
    final offline = api?.isOffline ?? false;

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              offline ? Icons.wifi_off : Icons.error_outline,
              size: 56,
              color: offline ? TillTheme.warning : TillTheme.danger,
            ),
            const SizedBox(height: 16),
            Text(
              api?.message ?? 'Could not load the catalogue.',
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.titleMedium,
            ),
            if (api?.isSuspended ?? false) ...[
              const SizedBox(height: 8),
              Text(
                'Contact your provider to restore access.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 16, color: TillTheme.muted),
              ),
            ],
            const SizedBox(height: 24),
            OutlinedButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Try again'),
            ),
          ],
        ),
      ),
    );
  }
}

/// The way into the cart, and until now there was not one.
///
/// [CartScreen] was written, tested and unreachable: nothing in the app
/// navigated to it. A cashier could look items up and had no way to sell one.
///
/// It lives in the app bar rather than on a floating button because a floating
/// button sits over the bottom of the list, which on a short phone is exactly
/// where the last item in a search result lands - so the thing being reached
/// for is the thing being covered.
class _CartButton extends ConsumerWidget {
  const _CartButton();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cart = ref.watch(cartControllerProvider);
    final count = cart.lines.length;

    return Stack(
      alignment: Alignment.center,
      children: [
        IconButton(
          tooltip: count == 0 ? 'Cart is empty' : 'Cart',
          iconSize: 28,
          icon: const Icon(Icons.shopping_cart_outlined),
          onPressed: () => Navigator.of(
            context,
          ).push(MaterialPageRoute<void>(builder: (_) => const CartScreen())),
        ),
        // Only when there is something to count. A badge reading zero is noise
        // a cashier learns to stop seeing, which costs it the one moment it
        // matters - noticing a line left over from the last customer.
        if (count > 0)
          Positioned(
            top: 6,
            right: 4,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              constraints: const BoxConstraints(minWidth: 20),
              decoration: BoxDecoration(
                color: TillTheme.warning,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                '$count',
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w700,
                  color: Colors.white,
                ),
              ),
            ),
          ),
      ],
    );
  }
}
