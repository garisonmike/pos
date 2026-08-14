/// What the cashier sees the moment a sale is finished.
///
/// The change owed is the whole point of this screen: it is what the cashier
/// counts out while the customer waits. Printing is offered rather than
/// automatic, because a shop with no printer paired must not be stopped, and a
/// failed print must never look like a failed sale.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../data/cart/checkout_service.dart';
import '../../data/models.dart';
import '../../data/printing/escpos.dart';

class SaleDoneScreen extends StatefulWidget {
  const SaleDoneScreen({
    super.key,
    required this.result,
    required this.printer,
    required this.buildReceipt,
    this.onDone,
  });

  final CheckoutResult result;
  final PrinterTransport printer;

  /// Injected so the screen does not have to know how a receipt is assembled -
  /// and so a test can drive it without a shop's branding to hand.
  final PrintableReceipt Function(CheckoutResult) buildReceipt;

  final VoidCallback? onDone;

  @override
  State<SaleDoneScreen> createState() => _SaleDoneScreenState();
}

class _SaleDoneScreenState extends State<SaleDoneScreen> {
  String? _printMessage;
  bool _printing = false;

  @override
  Widget build(BuildContext context) {
    final result = widget.result;

    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const SizedBox(height: 24),
              Icon(Icons.check_circle, size: 72, color: TillTheme.ok),
              const SizedBox(height: 16),
              const Text(
                'Sale complete',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 24, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 32),

              Text(
                'Change',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 18, color: TillTheme.muted),
              ),
              Text(
                Money(result.changeCents).format(),
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 48, fontWeight: FontWeight.w800),
              ),

              const SizedBox(height: 24),
              _Reference(result: result),

              const Spacer(),
              if (_printMessage != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Text(
                    _printMessage!,
                    textAlign: TextAlign.center,
                    style: TextStyle(color: TillTheme.muted),
                  ),
                ),
              OutlinedButton(
                onPressed: _printing ? null : _print,
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size.fromHeight(TillTheme.minTapTarget),
                ),
                child: Text(_printing ? 'Printing…' : 'Print receipt'),
              ),
              const SizedBox(height: 12),
              SizedBox(
                height: TillTheme.primaryActionHeight,
                child: FilledButton(
                  onPressed: widget.onDone ??
                      () => Navigator.of(context).popUntil((r) => r.isFirst),
                  child: const Text(
                    'Next customer',
                    style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _print() async {
    setState(() {
      _printing = true;
      _printMessage = null;
    });

    // A printer that is out of paper, unpaired or simply switched off must not
    // take the sale down with it. The money is already in the drawer and the
    // record is already made; printing is the one part that can be retried at
    // leisure.
    String message;
    try {
      if (!await widget.printer.isAvailable()) {
        message = 'No printer connected. The sale is saved.';
      } else {
        final bytes = renderEscPos(widget.buildReceipt(widget.result));
        await widget.printer.send(Uint8List.fromList(bytes));
        message = 'Printed.';
      }
    } catch (_) {
      message = 'The printer did not respond. The sale is saved.';
    }

    if (mounted) {
      setState(() {
        _printing = false;
        _printMessage = message;
      });
    }
  }
}

class _Reference extends StatelessWidget {
  const _Reference({required this.result});

  final CheckoutResult result;

  @override
  Widget build(BuildContext context) {
    if (!result.isQueued) {
      return Text(
        'Receipt ${result.reference}',
        textAlign: TextAlign.center,
        style: TextStyle(fontSize: 16, color: TillTheme.muted),
      );
    }

    // Said plainly. A cashier who thinks this is a final receipt number will
    // quote it to a customer who comes back, and it will match nothing.
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFFFFF4E5),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        children: [
          Text(
            'Temporary reference ${result.reference}',
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 4),
          Text(
            'This sale is saved on the till and will be sent when there is a '
            'connection. It gets its real receipt number then.',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 13, color: TillTheme.muted),
          ),
        ],
      ),
    );
  }
}
