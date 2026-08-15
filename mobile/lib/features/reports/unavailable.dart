/// What a report screen shows when it cannot reach the server.
///
/// **Its own state, never a blank screen and never zeroes.** Reports are
/// online-only by design - the outbox is for money that must not be lost, and a
/// report is regenerable - so a manager who opens one with no connection has to
/// be told that, plainly.
///
/// A spinner that never resolves, or an empty table, would both read as "a
/// quiet day". A wrong number that looks current is worse than no number, and
/// this is what stands in its place.
library;

import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../data/reports/reports_repository.dart';

class ReportsUnavailableView extends StatelessWidget {
  const ReportsUnavailableView({
    super.key,
    required this.failure,
    required this.onRetry,
  });

  final ReportsUnavailable failure;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              failure.isOffline ? Icons.cloud_off : Icons.error_outline,
              size: 56,
              color: TillTheme.muted,
            ),
            const SizedBox(height: 16),
            Text(
              failure.message,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            Text(
              // Said explicitly, because the alternative reading - that the
              // shop took nothing - is the one a manager would otherwise reach.
              'These figures are read from the server, so there is nothing to '
              'show until it can be reached. This is not a zero.',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 15, color: TillTheme.muted),
            ),
            const SizedBox(height: 8),
            Text(
              'Selling carries on as normal. Sales made now are saved on the '
              'till and will appear here once they sync.',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 14, color: TillTheme.muted),
            ),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Try again'),
              style: FilledButton.styleFrom(
                minimumSize: const Size(200, TillTheme.minTapTarget),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Renders one of the three states a report screen can be in.
///
/// Pulled out so every report screen handles unavailability the same way. One
/// screen quietly falling back to an empty list is exactly how the guarantee
/// above gets lost.
class ReportBody<T> extends StatelessWidget {
  const ReportBody({
    super.key,
    required this.snapshot,
    required this.onRetry,
    required this.builder,
  });

  final AsyncSnapshot<T> snapshot;
  final VoidCallback onRetry;
  final Widget Function(T data) builder;

  @override
  Widget build(BuildContext context) {
    if (snapshot.hasError) {
      final error = snapshot.error;
      if (error is ReportsUnavailable) {
        return ReportsUnavailableView(failure: error, onRetry: onRetry);
      }
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Text(
            'That report could not be loaded.',
            style: TextStyle(color: TillTheme.danger, fontSize: 16),
          ),
        ),
      );
    }

    if (!snapshot.hasData) {
      return const Center(child: CircularProgressIndicator());
    }

    return builder(snapshot.data as T);
  }
}
