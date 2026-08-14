/// Whether the till thinks it can reach the server, and what it shows for it.
///
/// Deliberately **not** a network-interface check. Android will happily report
/// a healthy Wi-Fi connection to a router with no route out, and a duka's
/// mobile data drops to something that resolves DNS but times out on a POST.
/// What matters to a cashier is whether the last thing the app actually tried
/// worked, so that is what this tracks.
///
/// It is a hint, never a gate. Selling continues in every state; the indicator
/// only tells the cashier what will happen to the receipt.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';

enum Reachability {
  /// The last request succeeded.
  online,

  /// The last request failed in a way that looks like connectivity.
  offline,

  /// Nothing has been tried yet since the app started.
  unknown,
}

class ConnectivityState {
  const ConnectivityState({
    this.reachability = Reachability.unknown,
    this.queuedSales = 0,
    this.stuckSales = 0,
    this.lastSyncedAt,
  });

  final Reachability reachability;

  /// Sales waiting to go up. Shown as a number because "some" is not something
  /// a shop owner can act on.
  final int queuedSales;

  /// Sales the server refused. These need a person, and they are counted
  /// separately so they cannot be mistaken for a queue that will drain itself.
  final int stuckSales;

  final DateTime? lastSyncedAt;

  bool get isOffline => reachability == Reachability.offline;
  bool get hasBacklog => queuedSales > 0;
  bool get needsAttention => stuckSales > 0;

  ConnectivityState copyWith({
    Reachability? reachability,
    int? queuedSales,
    int? stuckSales,
    DateTime? lastSyncedAt,
  }) =>
      ConnectivityState(
        reachability: reachability ?? this.reachability,
        queuedSales: queuedSales ?? this.queuedSales,
        stuckSales: stuckSales ?? this.stuckSales,
        lastSyncedAt: lastSyncedAt ?? this.lastSyncedAt,
      );
}

class ConnectivityController extends StateNotifier<ConnectivityState> {
  ConnectivityController() : super(const ConnectivityState());

  /// Record that a request came back.
  ///
  /// Only connectivity-shaped failures move the till offline. A 403 proves the
  /// server is reachable and answering, and showing "offline" for a refused
  /// discount would send a cashier looking for a network fault that is not
  /// there.
  void recordSuccess({DateTime? at}) => state = state.copyWith(
        reachability: Reachability.online,
        lastSyncedAt: at ?? DateTime.now(),
      );

  void recordFailure(ApiException error) {
    if (!error.isOffline) return;
    recordOffline();
  }

  /// Mark the till offline without an exception to point at.
  ///
  /// A sale that was queued because the cashier said the till is offline, or
  /// because a request failed in a way the app already predicted, is the same
  /// state - there is just no error object to inspect.
  void recordOffline() =>
      state = state.copyWith(reachability: Reachability.offline);

  void setBacklog({required int queued, required int stuck}) =>
      state = state.copyWith(queuedSales: queued, stuckSales: stuck);
}

final connectivityProvider =
    StateNotifierProvider<ConnectivityController, ConnectivityState>(
  (ref) => ConnectivityController(),
);

/// The strip that tells a cashier what will happen to this sale's receipt.
///
/// Shown as a persistent bar rather than a transient toast: a cashier who
/// looked away for the three seconds a snackbar lasts would carry on all
/// morning believing the receipts were final.
class OfflineBanner extends ConsumerWidget {
  const OfflineBanner({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(connectivityProvider);

    if (state.needsAttention) {
      return _Bar(
        colour: TillTheme.danger,
        icon: Icons.error_outline,
        message: '${state.stuckSales} sale${state.stuckSales == 1 ? '' : 's'} '
            'need checking',
        detail: 'The server would not take them. Show a manager.',
      );
    }

    if (state.isOffline) {
      return _Bar(
        colour: TillTheme.warning,
        icon: Icons.cloud_off,
        message: state.hasBacklog
            ? 'No connection · ${state.queuedSales} waiting to send'
            : 'No connection · sales are being saved here',
        detail: 'Keep selling. Receipts print with a temporary reference.',
      );
    }

    if (state.hasBacklog) {
      return _Bar(
        colour: TillTheme.warning,
        icon: Icons.cloud_upload_outlined,
        message: '${state.queuedSales} waiting to send',
        detail: 'These will go up on the next sync.',
      );
    }

    return const SizedBox.shrink();
  }
}

class _Bar extends StatelessWidget {
  const _Bar({
    required this.colour,
    required this.icon,
    required this.message,
    required this.detail,
  });

  final Color colour;
  final IconData icon;
  final String message;
  final String detail;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      color: colour,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      child: Row(
        children: [
          Icon(icon, color: Colors.white, size: 22),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  message,
                  style: const TextStyle(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                    fontSize: 15,
                  ),
                ),
                Text(
                  detail,
                  style: const TextStyle(color: Colors.white, fontSize: 13),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
