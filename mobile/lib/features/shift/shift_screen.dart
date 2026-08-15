/// Opening a drawer and counting it at close.
///
/// **The count is blind, and this screen has nothing to leak.** The API does
/// not report what an open drawer is expected to hold, so there is no figure
/// here to show a cashier before they declare theirs. That is the server's
/// rule; the screen is shaped by it rather than enforcing it, which is the
/// right way round - an interface that merely *chose* not to display a number
/// it had been sent would be one refactor away from displaying it.
///
/// The expected figure appears once, on the result of the close, when the count
/// is already committed and cannot be edited.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../data/models.dart';
import '../../data/reports/models.dart';
import '../../data/reports/reports_repository.dart';

class ShiftScreen extends StatefulWidget {
  const ShiftScreen({super.key, required this.repository});

  final ReportsRepository repository;

  @override
  State<ShiftScreen> createState() => _ShiftScreenState();
}

class _ShiftScreenState extends State<ShiftScreen> {
  late Future<OpenShift?> _current;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    setState(() {
      _current = widget.repository.currentShift();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Drawer')),
      body: FutureBuilder<OpenShift?>(
        future: _current,
        builder: (context, snapshot) {
          if (snapshot.hasError) {
            return _Problem(
              message: snapshot.error is ReportsUnavailable
                  ? (snapshot.error! as ReportsUnavailable).message
                  : 'The drawer could not be loaded.',
              onRetry: _load,
            );
          }
          if (!snapshot.hasData && snapshot.connectionState != ConnectionState.done) {
            return const Center(child: CircularProgressIndicator());
          }

          final shift = snapshot.data;
          if (shift == null) {
            return OpenDrawerForm(
              repository: widget.repository,
              onOpened: _load,
            );
          }
          return CloseDrawerForm(
            repository: widget.repository,
            shift: shift,
            onClosed: _load,
          );
        },
      ),
    );
  }
}

/// Starting a drawer with a counted float.
class OpenDrawerForm extends StatefulWidget {
  const OpenDrawerForm({
    super.key,
    required this.repository,
    required this.onOpened,
  });

  final ReportsRepository repository;
  final VoidCallback onOpened;

  @override
  State<OpenDrawerForm> createState() => _OpenDrawerFormState();
}

class _OpenDrawerFormState extends State<OpenDrawerForm> {
  /// Built up digit by digit as an integer of cents, never parsed from a
  /// decimal string. A double would eventually make an opening float that does
  /// not reconcile.
  int _floatCents = 0;
  bool _working = false;
  String? _error;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const SizedBox(height: 16),
          Text(
            'Count the cash in the drawer before you start selling.',
            style: TextStyle(fontSize: 16, color: TillTheme.muted),
          ),
          const SizedBox(height: 24),
          _AmountField(
            label: 'Opening float',
            cents: _floatCents,
            onChanged: (cents) => setState(() => _floatCents = cents),
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            Text(_error!, style: TextStyle(color: TillTheme.danger)),
          ],
          const SizedBox(height: 24),
          SizedBox(
            height: TillTheme.primaryActionHeight,
            child: FilledButton(
              onPressed: _working ? null : _open,
              child: Text(
                _working ? 'Opening…' : 'Open drawer',
                style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _open() async {
    setState(() {
      _working = true;
      _error = null;
    });
    try {
      await widget.repository.openShift(openingFloatCents: _floatCents);
      widget.onOpened();
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }
}

/// Counting the drawer at the end.
class CloseDrawerForm extends StatefulWidget {
  const CloseDrawerForm({
    super.key,
    required this.repository,
    required this.shift,
    required this.onClosed,
  });

  final ReportsRepository repository;
  final OpenShift shift;
  final VoidCallback onClosed;

  @override
  State<CloseDrawerForm> createState() => _CloseDrawerFormState();
}

class _CloseDrawerFormState extends State<CloseDrawerForm> {
  int _countedCents = 0;
  bool _working = false;
  String? _error;
  ClosedShift? _result;

  @override
  Widget build(BuildContext context) {
    if (_result != null) {
      return _CloseResult(result: _result!, onDone: widget.onClosed);
    }

    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: const Color(0xFFF3F4F5),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Drawer open', style: TextStyle(color: TillTheme.muted)),
                const SizedBox(height: 4),
                Text(
                  'Opened with ${Money(widget.shift.openingFloatCents).format()}',
                  style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          // Deliberately no expected figure anywhere above this field. There is
          // nothing to show: the API does not send one for an open drawer.
          Text(
            'Count everything in the drawer and enter the total. '
            'The till will tell you afterwards whether it matches.',
            style: TextStyle(fontSize: 15, color: TillTheme.muted),
          ),
          const SizedBox(height: 20),
          _AmountField(
            label: 'Counted in the drawer',
            cents: _countedCents,
            onChanged: (cents) => setState(() => _countedCents = cents),
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            Text(_error!, style: TextStyle(color: TillTheme.danger)),
          ],
          const SizedBox(height: 24),
          SizedBox(
            height: TillTheme.primaryActionHeight,
            child: FilledButton(
              onPressed: _working ? null : _close,
              child: Text(
                _working ? 'Closing…' : 'Close drawer',
                style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _close() async {
    setState(() {
      _working = true;
      _error = null;
    });
    try {
      final result = await widget.repository.closeShift(
        shiftId: widget.shift.id,
        declaredCents: _countedCents,
      );
      setState(() => _result = result);
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }
}

/// What the count came to, once it is committed.
class _CloseResult extends StatelessWidget {
  const _CloseResult({required this.result, required this.onDone});

  final ClosedShift result;
  final VoidCallback onDone;

  @override
  Widget build(BuildContext context) {
    final colour = result.balanced
        ? TillTheme.ok
        : (result.isShort ? TillTheme.danger : TillTheme.warning);

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const SizedBox(height: 24),
          Icon(
            result.balanced ? Icons.check_circle : Icons.error_outline,
            size: 64,
            color: colour,
          ),
          const SizedBox(height: 16),
          Text(
            result.balanced
                ? 'The drawer balances'
                : (result.isShort ? 'The drawer is short' : 'The drawer is over'),
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: colour),
          ),
          const SizedBox(height: 24),
          _Row(label: 'You counted', cents: result.declaredCents),
          _Row(label: 'Expected', cents: result.expectedCents),
          const Divider(height: 24),
          _Row(
            label: result.isShort ? 'Short by' : 'Difference',
            cents: result.varianceCents,
            emphasis: true,
            colour: colour,
          ),
          if (!result.balanced) ...[
            const SizedBox(height: 12),
            Text(
              // Said plainly, because a cashier who sees a variance will assume
              // they are in trouble. Recorded is not the same as blamed.
              'This has been recorded for a manager to look at. The count '
              'stands as you entered it.',
              style: TextStyle(fontSize: 14, color: TillTheme.muted),
            ),
          ],
          const Spacer(),
          SizedBox(
            height: TillTheme.primaryActionHeight,
            child: FilledButton(
              onPressed: onDone,
              child: const Text('Done', style: TextStyle(fontSize: 20)),
            ),
          ),
        ],
      ),
    );
  }
}

class _AmountField extends StatelessWidget {
  const _AmountField({
    required this.label,
    required this.cents,
    required this.onChanged,
  });

  final String label;
  final int cents;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: TextStyle(fontSize: 15, color: TillTheme.muted)),
        const SizedBox(height: 6),
        TextField(
          key: const Key('amount-field'),
          keyboardType: TextInputType.number,
          inputFormatters: [FilteringTextInputFormatter.digitsOnly],
          style: const TextStyle(fontSize: 28, fontWeight: FontWeight.w700),
          decoration: InputDecoration(
            prefixText: 'KES ',
            border: const OutlineInputBorder(),
            helperText: 'Entered in cents, so 5000 reads as ${Money(5000).format()}',
          ),
          onChanged: (text) => onChanged(int.tryParse(text) ?? 0),
        ),
        const SizedBox(height: 6),
        Text(
          Money(cents).format(),
          style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
        ),
      ],
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
  final int cents;
  final bool emphasis;
  final Color? colour;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            label,
            style: TextStyle(fontSize: emphasis ? 17 : 15, color: colour),
          ),
          Text(
            Money(cents).format(),
            style: TextStyle(
              fontSize: emphasis ? 22 : 16,
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

class _Problem extends StatelessWidget {
  const _Problem({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.cloud_off, size: 48, color: TillTheme.muted),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center, style: const TextStyle(fontSize: 17)),
            const SizedBox(height: 8),
            Text(
              // A drawer is a server-side record, so it cannot be opened or
              // closed offline. Selling is unaffected, and saying so stops a
              // cashier assuming the till is down.
              'A drawer is opened and closed on the server, so this needs a '
              'connection. Selling carries on either way.',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 14, color: TillTheme.muted),
            ),
            const SizedBox(height: 20),
            FilledButton(onPressed: onRetry, child: const Text('Try again')),
          ],
        ),
      ),
    );
  }
}
