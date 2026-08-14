/// Counting the cash and finishing the sale.
///
/// Two things here are shaped by what happens at a real counter.
///
/// **Change is the biggest thing on the screen.** It is the number the cashier
/// has to act on, with a customer watching and a queue behind them. Everything
/// else is reference.
///
/// **Quick-tender buttons for the notes that actually circulate.** A cashier
/// handed two thousand-shilling notes should press one button, not type five
/// digits. The exact-amount button is first because it is the commonest case.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../data/cart/cart_controller.dart';
import '../../data/cart/checkout_service.dart';
import '../../data/models.dart';
import 'connectivity.dart';

/// Notes a Kenyan shop actually sees, in cents.
const List<int> kCommonNotes = [5000, 10000, 20000, 50000, 100000];

class TenderScreen extends ConsumerStatefulWidget {
  const TenderScreen({super.key, required this.onCheckout});

  /// Injected rather than read from a provider so this screen can be driven in
  /// a test without a database and a network stack behind it.
  final Future<CheckoutResult> Function({
    required CartState cart,
    required int tenderedCents,
  }) onCheckout;

  @override
  ConsumerState<TenderScreen> createState() => _TenderScreenState();
}

class _TenderScreenState extends ConsumerState<TenderScreen> {
  /// What the cashier has typed, in cents. Held as an integer of cents built up
  /// digit by digit, never parsed from a decimal string - a double would
  /// eventually make a total that does not add up.
  int _tenderedCents = 0;
  bool _working = false;

  @override
  Widget build(BuildContext context) {
    final cart = ref.watch(cartControllerProvider);
    final due = cart.totals.cashDueCents;
    final short = _tenderedCents < due;
    final change = short ? 0 : _tenderedCents - due;

    return Scaffold(
      appBar: AppBar(title: const Text('Payment')),
      body: Column(
        children: [
          const OfflineBanner(),
          // The figures scroll and the keypad does not. On a short screen -
          // a phone in landscape, a small tablet - something has to give, and
          // it must not be the keys or the Finish button: a cashier who cannot
          // reach them cannot take the money at all.
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                children: [
                  Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      children: [
                        _Figure(label: 'Due', cents: due),
                        const SizedBox(height: 8),
                        _Figure(
                          label: 'Cash received',
                          cents: _tenderedCents,
                          emphasis: false,
                        ),
                        const SizedBox(height: 16),
                        _ChangeCard(changeCents: change, short: short),
                      ],
                    ),
                  ),
                  _QuickTender(
                    dueCents: due,
                    onPick: (cents) => setState(() => _tenderedCents = cents),
                    onAdd: (cents) => setState(() => _tenderedCents += cents),
                  ),
                ],
              ),
            ),
          ),
          _Keypad(
            onDigit: (digit) => setState(() {
              // Built up as cents, so 1 2 3 4 reads as 12.34. Capped so a stuck
              // finger cannot produce a figure that overflows the display and
              // hides what is really there.
              final next = _tenderedCents * 10 + digit;
              if (next <= 100000000) _tenderedCents = next;
            }),
            onBackspace: () => setState(() => _tenderedCents ~/= 10),
            onClear: () => setState(() => _tenderedCents = 0),
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: SafeArea(
              top: false,
              child: SizedBox(
                width: double.infinity,
                height: TillTheme.primaryActionHeight,
                child: FilledButton(
                  onPressed: short || _working ? null : _finish,
                  child: _working
                      ? const SizedBox(
                          height: 24,
                          width: 24,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white,
                          ),
                        )
                      : Text(
                          short ? 'Not enough' : 'Finish sale',
                          style: const TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _finish() async {
    setState(() => _working = true);
    final cart = ref.read(cartControllerProvider);

    try {
      final result = await widget.onCheckout(
        cart: cart,
        tenderedCents: _tenderedCents,
      );

      // A queued sale means the request did not get through, whatever the
      // reason. A refusal proves the opposite - the server answered - so it
      // must not put the till into the offline state and send a cashier
      // hunting for a network fault that is not there.
      final connectivity = ref.read(connectivityProvider.notifier);
      if (result.isQueued) {
        connectivity.recordOffline();
      } else if (!result.isRefused) {
        connectivity.recordSuccess();
      }

      if (!mounted) return;

      if (result.isRefused) {
        // The sale did not happen and the cart is kept, so the cashier can fix
        // whatever the server objected to while the customer is still there.
        _showRefusal(result);
        return;
      }

      ref.read(cartControllerProvider.notifier).clear();
      await Navigator.of(context).pushReplacementNamed(
        '/sell/done',
        arguments: result,
      );
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }

  void _showRefusal(CheckoutResult result) {
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('The sale was not accepted'),
        content: Text(
          result.error?.message ??
              'The server would not take this sale. Nothing has been charged.',
        ),
        actions: [
          FilledButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Go back'),
          ),
        ],
      ),
    );
  }
}

class _Figure extends StatelessWidget {
  const _Figure({required this.label, required this.cents, this.emphasis = true});

  final String label;
  final int cents;
  final bool emphasis;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        Text(label, style: TextStyle(fontSize: 16, color: TillTheme.muted)),
        Text(
          Money(cents).format(),
          style: TextStyle(
            fontSize: emphasis ? 26 : 22,
            fontWeight: emphasis ? FontWeight.w800 : FontWeight.w600,
            fontFeatures: const [FontFeature.tabularFigures()],
          ),
        ),
      ],
    );
  }
}

class _ChangeCard extends StatelessWidget {
  const _ChangeCard({required this.changeCents, required this.short});

  final int changeCents;
  final bool short;

  @override
  Widget build(BuildContext context) {
    // The number the cashier acts on, so it is the largest thing on the screen.
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(vertical: 20, horizontal: 16),
      decoration: BoxDecoration(
        color: short ? const Color(0xFFF3F4F5) : const Color(0xFFE8F5E9),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        children: [
          Text(
            short ? 'Still to pay' : 'Change',
            style: TextStyle(fontSize: 16, color: TillTheme.muted),
          ),
          const SizedBox(height: 4),
          Text(
            Money(changeCents).format(),
            style: TextStyle(
              fontSize: 40,
              fontWeight: FontWeight.w800,
              color: short ? TillTheme.muted : TillTheme.ok,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}

class _QuickTender extends StatelessWidget {
  const _QuickTender({
    required this.dueCents,
    required this.onPick,
    required this.onAdd,
  });

  final int dueCents;
  final ValueChanged<int> onPick;
  final ValueChanged<int> onAdd;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: TillTheme.minTapTarget + 8,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        children: [
          // First, because a customer paying the exact amount is the commonest
          // case by a wide margin.
          _Chip(label: 'Exact', onTap: () => onPick(dueCents)),
          for (final note in kCommonNotes)
            _Chip(
              label: Money(note).format(currency: ''),
              onTap: () => onAdd(note),
            ),
        ],
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
      child: OutlinedButton(
        onPressed: () {
          HapticFeedback.selectionClick();
          onTap();
        },
        style: OutlinedButton.styleFrom(
          minimumSize: const Size(88, TillTheme.minTapTarget),
        ),
        child: Text(label, style: const TextStyle(fontSize: 16)),
      ),
    );
  }
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
      padding: const EdgeInsets.symmetric(horizontal: 12),
      child: Column(
        children: [
          for (final row in const [
            [1, 2, 3],
            [4, 5, 6],
            [7, 8, 9],
          ])
            Row(
              children: [
                for (final digit in row) Expanded(child: _Key(digit: digit, onDigit: onDigit)),
              ],
            ),
          Row(
            children: [
              Expanded(
                child: _KeyButton(
                  label: 'C',
                  onPressed: onClear,
                  semantic: 'Clear',
                ),
              ),
              Expanded(child: _Key(digit: 0, onDigit: onDigit)),
              Expanded(
                child: _KeyButton(
                  icon: Icons.backspace_outlined,
                  onPressed: onBackspace,
                  semantic: 'Backspace',
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
  const _Key({required this.digit, required this.onDigit});

  final int digit;
  final ValueChanged<int> onDigit;

  @override
  Widget build(BuildContext context) => _KeyButton(
        label: '$digit',
        semantic: '$digit',
        onPressed: () => onDigit(digit),
      );
}

class _KeyButton extends StatelessWidget {
  const _KeyButton({
    this.label,
    this.icon,
    required this.onPressed,
    required this.semantic,
  });

  final String? label;
  final IconData? icon;
  final VoidCallback onPressed;
  final String semantic;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: semantic,
      button: true,
      child: Padding(
        padding: const EdgeInsets.all(4),
        child: SizedBox(
          height: TillTheme.minTapTarget + 8,
          child: OutlinedButton(
            onPressed: () {
              HapticFeedback.selectionClick();
              onPressed();
            },
            child: icon != null
                ? Icon(icon, size: 24)
                : Text(
                    label!,
                    style: const TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
          ),
        ),
      ),
    );
  }
}
