/// The shift summary a manager reads at close.
///
/// This is the screen where "today's sales" and "the drawer" are compared, and
/// the one place the tie-out has to be got right visually as well as
/// structurally.
///
/// **Three things stay apart, on the glass as in the API.**
///
/// *Counted* is what somebody signed for. It sits in its own block and never
/// changes.
///
/// *Arrived after close* sits in a separate, differently coloured block. It is
/// what turned up later.
///
/// *If those had landed in time* is shown as a **secondary** figure with that
/// wording, never as a variance. An explanation sitting next to a variance is
/// fine; folded into it, it would be a third number that is neither what was
/// signed for nor what the sales say.
library;

import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../data/models.dart';
import '../../data/reports/models.dart';
import '../../data/reports/reports_repository.dart';
import 'unavailable.dart';

class DrawerReportScreen extends StatefulWidget {
  const DrawerReportScreen({super.key, required this.repository});

  final ReportsRepository repository;

  @override
  State<DrawerReportScreen> createState() => _DrawerReportScreenState();
}

class _DrawerReportScreenState extends State<DrawerReportScreen> {
  late Future<DrawerReport> _report;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    // A block body: an arrow would return the Future from the closure, which
    // Flutter refuses.
    setState(() {
      _report = widget.repository.drawers();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Drawers')),
      body: FutureBuilder<DrawerReport>(
        future: _report,
        builder: (context, snapshot) => ReportBody<DrawerReport>(
          snapshot: snapshot,
          onRetry: _load,
          builder: (report) => _Report(report: report),
        ),
      ),
    );
  }
}

class _Report extends StatelessWidget {
  const _Report({required this.report});

  final DrawerReport report;

  @override
  Widget build(BuildContext context) {
    if (report.shifts.isEmpty) {
      return Center(
        child: Text(
          'No drawers were opened in this period.',
          style: TextStyle(color: TillTheme.muted, fontSize: 16),
        ),
      );
    }

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _PeriodHeader(report: report),
        const SizedBox(height: 16),
        for (final shift in report.shifts) ...[
          _ShiftCard(shift: shift),
          const SizedBox(height: 12),
        ],
        if (report.note.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            report.note,
            style: TextStyle(fontSize: 13, color: TillTheme.muted),
          ),
        ],
      ],
    );
  }
}

class _PeriodHeader extends StatelessWidget {
  const _PeriodHeader({required this.report});

  final DrawerReport report;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          report.label,
          style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 4),
        // The figure the drawers are compared against. Named plainly, because a
        // manager who does not know this exists will assume the drawers should
        // add up to it exactly.
        Text(
          'Cash taken in this period: ${Money(report.cashTakenCents).format()}',
          style: TextStyle(fontSize: 15, color: TillTheme.muted),
        ),
        if (report.unreconciledCount > 0) ...[
          const SizedBox(height: 8),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFFFFF4E5),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Row(
              children: [
                Icon(Icons.info_outline, size: 20, color: TillTheme.warning),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    '${report.unreconciledCount} drawer'
                    '${report.unreconciledCount == 1 ? '' : 's'} had sales arrive '
                    'after closing. The counted figures below are unchanged.',
                    style: const TextStyle(fontSize: 14),
                  ),
                ),
              ],
            ),
          ),
        ],
      ],
    );
  }
}

class _ShiftCard extends StatelessWidget {
  const _ShiftCard({required this.shift});

  final DrawerReconciliation shift;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        border: Border.all(color: TillTheme.line),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 8),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    shift.cashier,
                    style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700),
                  ),
                ),
                if (shift.isOpen)
                  Chip(
                    label: const Text('Still open'),
                    backgroundColor: const Color(0xFFE8F5E9),
                  ),
              ],
            ),
          ),

          // ---- Counted. Signed for, never recomputed. --------------------
          _Block(
            title: 'Counted',
            background: Colors.white,
            children: [
              _Row(label: 'Opening float', cents: shift.openingFloatCents),
              if (shift.declaredClosingCents != null)
                _Row(label: 'Counted at close', cents: shift.declaredClosingCents!),
              if (shift.expectedClosingCents != null)
                _Row(label: 'Expected', cents: shift.expectedClosingCents!),
              if (shift.varianceCents != null)
                _Row(
                  label: shift.isShort ? 'Short by' : (shift.isOver ? 'Over by' : 'Variance'),
                  cents: shift.varianceCents!,
                  emphasis: true,
                  colour: shift.varianceCents == 0
                      ? TillTheme.ok
                      : (shift.isShort ? TillTheme.danger : TillTheme.warning),
                ),
              if (shift.isOpen)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: Text(
                    'Not counted yet. There is no variance until somebody counts.',
                    style: TextStyle(fontSize: 13, color: TillTheme.muted),
                  ),
                ),
            ],
          ),

          // ---- Arrived after close. Its own block, its own colour. -------
          //
          // Visually separate because it is a different kind of fact. Merging
          // it into the block above would put a number a person signed for
          // beside one that arrived without them.
          if (shift.lateCount > 0)
            _Block(
              title: 'Arrived after close',
              background: const Color(0xFFFFF4E5),
              children: [
                _Row(label: 'Cash', cents: shift.lateCashCents),
                _Row(
                  label: '${shift.lateCount} sale'
                      '${shift.lateCount == 1 ? '' : 's'} synced late',
                  cents: null,
                ),
                const SizedBox(height: 6),
                if (shift.explainedVarianceCents != null)
                  Text(
                    // Deliberately worded as a hypothetical, and rendered as a
                    // secondary line rather than as a figure in the counted
                    // block. It explains the gap; it does not correct it.
                    'Had those landed in time, the variance would have read '
                    '${Money(shift.explainedVarianceCents!).format()}. '
                    'The counted figures above are unchanged.',
                    style: TextStyle(fontSize: 13, color: TillTheme.muted),
                  ),
              ],
            ),

          if (shift.lateCount == 0 && !shift.isOpen)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 14),
              child: Row(
                children: [
                  Icon(Icons.check_circle_outline, size: 18, color: TillTheme.ok),
                  const SizedBox(width: 8),
                  Text(
                    'Nothing arrived after this drawer closed.',
                    style: TextStyle(fontSize: 13, color: TillTheme.muted),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _Block extends StatelessWidget {
  const _Block({
    required this.title,
    required this.background,
    required this.children,
  });

  final String title;
  final Color background;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      color: background,
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title.toUpperCase(),
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.8,
              color: TillTheme.muted,
            ),
          ),
          const SizedBox(height: 6),
          ...children,
        ],
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({
    required this.label,
    required this.cents,
    this.emphasis = false,
    this.colour,
  });

  final String label;
  final int? cents;
  final bool emphasis;
  final Color? colour;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            label,
            style: TextStyle(
              fontSize: emphasis ? 16 : 15,
              fontWeight: emphasis ? FontWeight.w700 : FontWeight.w400,
              color: colour,
            ),
          ),
          if (cents != null)
            Text(
              Money(cents!).format(),
              style: TextStyle(
                fontSize: emphasis ? 18 : 15,
                fontWeight: emphasis ? FontWeight.w800 : FontWeight.w600,
                color: colour,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
        ],
      ),
    );
  }
}
