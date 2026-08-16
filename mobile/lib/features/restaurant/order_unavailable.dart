/// What a waiter sees when the order cannot be sent.
///
/// **Orders are server-side only**, deliberately: order state shared across
/// several tablets with no connection is a distributed-systems problem this
/// module did not take on. That decision has one cost, and this is the guard
/// against it.
///
/// A waiter who has typed six lines and taps *Send* must not get a spinner that
/// never resolves, or a silent failure that looks like success. Either would
/// lose the order and, worse, would lose it invisibly - the food never reaches
/// the kitchen and nobody finds out until a table asks where dinner is.
///
/// So: an explicit state, saying what happened, that **the order is still on
/// this screen**, and what to do. Nothing is cleared until the server has it.
library;

import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';

/// Whether a failure means "the server never heard this".
bool looksLikeNoConnection(ApiException error) =>
    error.isOffline || error.status == 0 || error.status >= 500;

class OrderUnavailableBanner extends StatelessWidget {
  const OrderUnavailableBanner({
    super.key,
    required this.error,
    required this.onRetry,
    this.lineCount = 0,
  });

  final ApiException error;
  final VoidCallback onRetry;

  /// How much the waiter has typed. Named in the message, because "your order
  /// is safe" is only reassuring if it says how much of it.
  final int lineCount;

  @override
  Widget build(BuildContext context) {
    final offline = looksLikeNoConnection(error);

    return Container(
      width: double.infinity,
      color: offline ? TillTheme.warning : TillTheme.danger,
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                offline ? Icons.cloud_off : Icons.error_outline,
                color: Colors.white,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  offline
                      ? 'No connection, so this order cannot be sent right now.'
                      : (error.message),
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 16,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            // The reassurance a waiter actually needs, and the reason this is
            // a banner rather than a dialog that can be dismissed by accident.
            lineCount > 0
                ? 'The $lineCount line${lineCount == 1 ? '' : 's'} you have typed '
                    'are still here. Nothing has been sent to the kitchen and '
                    'nothing has been lost.'
                : 'Nothing has been sent to the kitchen.',
            style: const TextStyle(color: Colors.white, fontSize: 14),
          ),
          if (offline) ...[
            const SizedBox(height: 4),
            Text(
              // Said plainly, because a waiter who assumes it will sync later -
              // as sales do - will walk away from the tablet.
              'Orders are not saved on the tablet. Stay on this screen and try '
              'again once the connection is back.',
              style: const TextStyle(color: Colors.white, fontSize: 14),
            ),
          ],
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            height: TillTheme.minTapTarget,
            child: FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Try sending again'),
              style: FilledButton.styleFrom(
                backgroundColor: Colors.white,
                foregroundColor: offline ? TillTheme.warning : TillTheme.danger,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// The whole-screen version, for when the floor itself cannot be loaded.
class OrdersUnavailableView extends StatelessWidget {
  const OrdersUnavailableView({
    super.key,
    required this.error,
    required this.onRetry,
  });

  final ApiException error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final offline = looksLikeNoConnection(error);

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              offline ? Icons.cloud_off : Icons.error_outline,
              size: 56,
              color: TillTheme.muted,
            ),
            const SizedBox(height: 16),
            Text(
              offline
                  ? 'No connection, so the tables cannot be shown.'
                  : 'The tables could not be loaded.',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            Text(
              // Distinguished from an empty floor, which looks identical and
              // means the opposite.
              'This is not an empty restaurant. Orders live on the server, so '
              'there is nothing to show until it can be reached.',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 15, color: TillTheme.muted),
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
