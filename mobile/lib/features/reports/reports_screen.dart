/// What a manager reads on the till.
///
/// Deliberately thin. The till is a selling device; this is today's takings,
/// what moved, and who was on the counter - not the full back office. Anything
/// that wants a wide table belongs on a screen with a keyboard in front of it.
///
/// Every tab is online-only and says so when it cannot reach the server. See
/// `unavailable.dart` for why that is a state rather than an empty list.
library;

import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../data/models.dart';
import '../../data/reports/models.dart';
import '../../data/reports/reports_repository.dart';
import 'unavailable.dart';

/// Which window a report covers.
class ReportRange {
  const ReportRange(this.granularity, this.label);

  static const today = ReportRange('day', 'Today');
  static const week = ReportRange('week', 'This week');
  static const month = ReportRange('month', 'This month');
  static const all = [today, week, month];

  final String granularity;
  final String label;
}

class ReportsScreen extends StatefulWidget {
  const ReportsScreen({super.key, required this.repository});

  final ReportsRepository repository;

  @override
  State<ReportsScreen> createState() => _ReportsScreenState();
}

class _ReportsScreenState extends State<ReportsScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(length: 3, vsync: this);
  ReportRange _range = ReportRange.today;

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Reports'),
        bottom: TabBar(
          controller: _tabs,
          tabs: const [
            Tab(text: 'Takings'),
            Tab(text: 'Best sellers'),
            Tab(text: 'Cashiers'),
          ],
        ),
      ),
      body: Column(
        children: [
          _RangePicker(
            selected: _range,
            onSelect: (range) => setState(() => _range = range),
          ),
          Expanded(
            child: TabBarView(
              controller: _tabs,
              children: [
                SalesTab(repository: widget.repository, range: _range),
                BestSellersTab(repository: widget.repository, range: _range),
                CashiersTab(repository: widget.repository, range: _range),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _RangePicker extends StatelessWidget {
  const _RangePicker({required this.selected, required this.onSelect});

  final ReportRange selected;
  final ValueChanged<ReportRange> onSelect;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: [
          for (final range in ReportRange.all)
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: ChoiceChip(
                label: Text(range.label),
                selected: range.granularity == selected.granularity,
                onSelected: (_) => onSelect(range),
              ),
            ),
        ],
      ),
    );
  }
}

/// A tab that reloads whenever the range changes.
abstract class _ReportTab<T> extends StatefulWidget {
  const _ReportTab({super.key, required this.repository, required this.range});

  final ReportsRepository repository;
  final ReportRange range;
}

class SalesTab extends _ReportTab<List<SalesSummary>> {
  const SalesTab({super.key, required super.repository, required super.range});

  @override
  State<SalesTab> createState() => _SalesTabState();
}

class _SalesTabState extends State<SalesTab> {
  late Future<List<SalesSummary>> _future;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(SalesTab old) {
    super.didUpdateWidget(old);
    if (old.range.granularity != widget.range.granularity) _load();
  }

  void _load() {
    // A block body, not an arrow: `setState(() => _future = ...)` returns the
    // Future from the closure and Flutter refuses it.
    setState(() {
      _future = widget.repository.sales(granularity: widget.range.granularity);
    });
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<SalesSummary>>(
      future: _future,
      builder: (context, snapshot) => ReportBody<List<SalesSummary>>(
        snapshot: snapshot,
        onRetry: _load,
        builder: (summaries) {
          if (summaries.isEmpty) {
            return const _Empty(message: 'Nothing sold in this period.');
          }
          final summary = summaries.first;
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              _Headline(
                label: 'Taken',
                cents: summary.taken.totalCents,
                caption: '${summary.saleCount} sale'
                    '${summary.saleCount == 1 ? '' : 's'}',
              ),
              const SizedBox(height: 16),
              // Cash on its own, because it is what reconciles against a
              // drawer somebody counted. The server keeps them apart; showing
              // one merged figure would undo that.
              _Line(label: 'Cash', cents: summary.taken.cashCents),
              _Line(label: 'M-Pesa', cents: summary.taken.mpesaCents),
              const Divider(height: 24),
              _Line(label: 'Gross', cents: summary.grossCents),
              _Line(label: 'VAT', cents: summary.taxCents),
              _Line(label: 'Discounts', cents: summary.discountCents),
              const Divider(height: 24),
              _Line(
                label: 'Refunded (${summary.refundCount})',
                cents: summary.refunded.totalCents,
              ),
              _Line(label: 'Refund rate', text: summary.refundRate),
              _Line(label: 'Average basket', cents: summary.averageBasketCents),
              if (summary.voidCount > 0)
                _Line(label: 'Voids', text: '${summary.voidCount}'),
              if (summary.offlineSaleCount > 0)
                _Line(
                  label: 'Rung up offline',
                  text: '${summary.offlineSaleCount}',
                ),
            ],
          );
        },
      ),
    );
  }
}

class BestSellersTab extends _ReportTab<List<BestSeller>> {
  const BestSellersTab({
    super.key,
    required super.repository,
    required super.range,
  });

  @override
  State<BestSellersTab> createState() => _BestSellersTabState();
}

class _BestSellersTabState extends State<BestSellersTab> {
  late Future<List<BestSeller>> _future;
  String _order = 'revenue';

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(BestSellersTab old) {
    super.didUpdateWidget(old);
    if (old.range.granularity != widget.range.granularity) _load();
  }

  void _load() {
    setState(() {
      _future = widget.repository.bestSellers(
        granularity: widget.range.granularity,
        order: _order,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        // Both rankings offered, because they answer different questions: a
        // crate of matchboxes outsells everything and earns almost nothing.
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'revenue', label: Text('By revenue')),
              ButtonSegment(value: 'quantity', label: Text('By quantity')),
            ],
            selected: {_order},
            onSelectionChanged: (selection) {
              setState(() => _order = selection.first);
              _load();
            },
          ),
        ),
        Expanded(
          child: FutureBuilder<List<BestSeller>>(
            future: _future,
            builder: (context, snapshot) => ReportBody<List<BestSeller>>(
              snapshot: snapshot,
              onRetry: _load,
              builder: (sellers) {
                if (sellers.isEmpty) {
                  return const _Empty(message: 'Nothing sold in this period.');
                }
                return ListView.separated(
                  itemCount: sellers.length,
                  separatorBuilder: (_, __) =>
                      Divider(height: 1, color: TillTheme.line),
                  itemBuilder: (context, index) {
                    final seller = sellers[index];
                    return ListTile(
                      title: Text(seller.name),
                      subtitle: Text('${seller.quantityDisplay} sold'),
                      trailing: Text(
                        Money(seller.revenueCents).format(),
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    );
                  },
                );
              },
            ),
          ),
        ),
      ],
    );
  }
}

class CashiersTab extends _ReportTab<CashierReport> {
  const CashiersTab({super.key, required super.repository, required super.range});

  @override
  State<CashiersTab> createState() => _CashiersTabState();
}

class _CashiersTabState extends State<CashiersTab> {
  late Future<CashierReport> _future;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(CashiersTab old) {
    super.didUpdateWidget(old);
    if (old.range.granularity != widget.range.granularity) _load();
  }

  void _load() {
    setState(() {
      _future = widget.repository.cashiers(granularity: widget.range.granularity);
    });
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<CashierReport>(
      future: _future,
      builder: (context, snapshot) => ReportBody<CashierReport>(
        snapshot: snapshot,
        onRetry: _load,
        builder: (report) {
          if (report.cashiers.isEmpty) {
            return const _Empty(message: 'Nobody rang anything up in this period.');
          }
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              // The note the server sends, rendered rather than dropped. The
              // framing is part of the report: a rate without its denominator
              // supports a conclusion the data does not.
              if (report.note.isNotEmpty)
                Container(
                  padding: const EdgeInsets.all(12),
                  margin: const EdgeInsets.only(bottom: 16),
                  decoration: BoxDecoration(
                    color: const Color(0xFFF3F4F5),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(Icons.info_outline, size: 18, color: TillTheme.muted),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          report.note,
                          style: TextStyle(fontSize: 13, color: TillTheme.muted),
                        ),
                      ),
                    ],
                  ),
                ),
              for (final cashier in report.cashiers) ...[
                _CashierCard(cashier: cashier),
                const SizedBox(height: 12),
              ],
            ],
          );
        },
      ),
    );
  }
}

class _CashierCard extends StatelessWidget {
  const _CashierCard({required this.cashier});

  final CashierFigures cashier;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        border: Border.all(color: TillTheme.line),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            cashier.displayName,
            style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 8),
          _Line(label: 'Sales', text: '${cashier.saleCount}'),
          _Line(label: 'Took', cents: cashier.grossCents),
          _Line(label: 'Average basket', cents: cashier.averageBasketCents),
          // The denominator beside the rate, always. Shown as "n of m", so how
          // thin the evidence is stays visible - two discounted sales out of
          // three is not a pattern.
          _Line(
            label: 'Discounted',
            text: '${cashier.discountedSaleCount} of ${cashier.saleCount}'
                ' · ${cashier.discountRate}',
          ),
          if (cashier.voidCount > 0)
            _Line(label: 'Voids', text: '${cashier.voidCount}'),
          if (cashier.refundCount > 0)
            _Line(label: 'Refunds', text: '${cashier.refundCount}'),
        ],
      ),
    );
  }
}

class _Headline extends StatelessWidget {
  const _Headline({required this.label, required this.cents, required this.caption});

  final String label;
  final int cents;
  final String caption;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: TextStyle(fontSize: 16, color: TillTheme.muted)),
        Text(
          Money(cents).format(),
          style: const TextStyle(fontSize: 36, fontWeight: FontWeight.w800),
        ),
        Text(caption, style: TextStyle(fontSize: 14, color: TillTheme.muted)),
      ],
    );
  }
}

class _Line extends StatelessWidget {
  const _Line({required this.label, this.cents, this.text});

  final String label;
  final int? cents;
  final String? text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Flexible(child: Text(label, style: const TextStyle(fontSize: 15))),
          Text(
            text ?? Money(cents ?? 0).format(),
            style: const TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.w600,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Text(message, style: TextStyle(fontSize: 16, color: TillTheme.muted)),
    );
  }
}
