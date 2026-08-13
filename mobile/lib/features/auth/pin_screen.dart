import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../providers.dart';
import 'password_screen.dart';
import 'widgets.dart';

/// Fast sign-in at the counter.
///
/// The screen a shop actually lives on. A cashier takes over mid-shift, types
/// four digits and is in - no password, no keyboard. That speed is the whole
/// reason PIN sign-in exists: a password typed on a tablet between customers is
/// friction, and friction is how a shop ends up sharing one login and losing
/// any idea of who took the money.
///
/// The four digits are only acceptable because the till itself is the other
/// half of the credential, and because the server locks the device after a
/// handful of wrong tries. This screen shows how many are left, so a cashier
/// who has fumbled twice knows before they are locked out rather than after.
class PinScreen extends ConsumerStatefulWidget {
  const PinScreen({super.key});

  @override
  ConsumerState<PinScreen> createState() => _PinScreenState();
}

class _PinScreenState extends ConsumerState<PinScreen> {
  final _username = TextEditingController();
  String _pin = '';
  bool _busy = false;
  String? _error;
  int? _attemptsRemaining;
  bool _lockedOut = false;

  @override
  void initState() {
    super.initState();
    // Pre-fill whoever signed in last. Saves typing at a busy counter, and
    // gives away nothing that holding the till does not already give away.
    ref.read(lastUsernameProvider.future).then((name) {
      if (name != null && mounted) _username.text = name;
    });
  }

  @override
  void dispose() {
    _username.dispose();
    super.dispose();
  }

  void _press(String digit) {
    if (_pin.length >= 4 || _busy || _lockedOut) return;

    HapticFeedback.selectionClick();
    setState(() {
      _pin += digit;
      _error = null;
    });

    // Four digits is the whole PIN, so submit rather than making someone reach
    // for a separate button they would have to look at.
    if (_pin.length == 4) _submit();
  }

  void _backspace() {
    if (_pin.isEmpty || _busy) return;
    HapticFeedback.selectionClick();
    setState(() => _pin = _pin.substring(0, _pin.length - 1));
  }

  Future<void> _submit() async {
    if (_username.text.trim().isEmpty) {
      setState(() {
        _error = 'Enter your username first';
        _pin = '';
      });
      return;
    }

    setState(() {
      _busy = true;
      _error = null;
    });

    try {
      await ref
          .read(authRepositoryProvider)
          .signInWithPin(username: _username.text.trim(), pin: _pin);
      await ref.read(tillStateProvider.notifier).refresh();
    } on ApiException catch (error) {
      if (!mounted) return;
      HapticFeedback.heavyImpact();
      setState(() {
        _pin = '';
        _error = error.message;
        _lockedOut = error.isLockedOut;
        _attemptsRemaining = null;
      });

      // The server tells us how many tries are left; showing it is what turns a
      // lockout from a surprise into a warning.
      final remaining = error.fields?['attempts_remaining'];
      if (remaining is int) setState(() => _attemptsRemaining = remaining);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final tenantName = ref.watch(tillStateProvider).value?.tenantName;

    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Column(
            children: [
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Text(
                      tenantName?.isNotEmpty == true ? tenantName! : 'Till',
                      style: text.titleLarge,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  TextButton(
                    onPressed: _busy ? null : _usePassword,
                    style: TextButton.styleFrom(
                      minimumSize: const Size(88, TillTheme.minTapTarget),
                    ),
                    child: const Text('Use password'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _username,
                autocorrect: false,
                textCapitalization: TextCapitalization.none,
                decoration: const InputDecoration(
                  labelText: 'Your username',
                  prefixIcon: Icon(Icons.person_outline),
                ),
              ),
              const SizedBox(height: 24),

              if (_error != null)
                ErrorBanner(
                  message: _attemptsRemaining != null
                      ? '$_error  ($_attemptsRemaining ${_attemptsRemaining == 1 ? 'try' : 'tries'} left)'
                      : _error!,
                  icon: _lockedOut ? Icons.lock_clock : Icons.error_outline,
                )
              else
                Text(
                  'Enter your 4-digit PIN',
                  style: text.bodyLarge?.copyWith(color: TillTheme.muted),
                ),

              const SizedBox(height: 24),
              if (_busy)
                const SizedBox(
                  height: 20,
                  child: Center(
                    child: SizedBox(
                      height: 20,
                      width: 20,
                      child: CircularProgressIndicator(strokeWidth: 3),
                    ),
                  ),
                )
              else
                PinDots(length: _pin.length),

              const Spacer(),
              _keypad(),
              const SizedBox(height: 16),
            ],
          ),
        ),
      ),
    );
  }

  Widget _keypad() {
    // The pad sits at the bottom so it falls under a thumb, and is laid out
    // like a phone keypad because that is the muscle memory people already
    // have for typing digits.
    const rows = [
      ['1', '2', '3'],
      ['4', '5', '6'],
      ['7', '8', '9'],
    ];

    return Column(
      children: [
        for (final row in rows)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Row(
              children: [
                for (final digit in row) ...[
                  Expanded(child: PinKey(label: digit, onTap: () => _press(digit))),
                  if (digit != row.last) const SizedBox(width: 12),
                ],
              ],
            ),
          ),
        Row(
          children: [
            const Expanded(child: SizedBox(height: 72)),
            const SizedBox(width: 12),
            Expanded(child: PinKey(label: '0', onTap: () => _press('0'))),
            const SizedBox(width: 12),
            Expanded(
              child: PinKey(
                label: 'Delete',
                icon: Icons.backspace_outlined,
                onTap: _backspace,
              ),
            ),
          ],
        ),
      ],
    );
  }

  Future<void> _usePassword() async {
    // Signing out of the *device* is not what is wanted here - just a route to
    // the password screen, which the router shows when there is no session and
    // the caller asks for it.
    await ref.read(sessionStoreProvider).clear();
    if (mounted) {
      Navigator.of(context).push(
        MaterialPageRoute<void>(
          builder: (_) => const _PasswordFallback(),
        ),
      );
    }
  }
}

/// The password screen, reached from the PIN pad.
///
/// A separate route rather than a stage change, so that arriving here does not
/// un-register the till: the device token stays put and PIN sign-in still works
/// afterwards. It pops itself once a session exists.
class _PasswordFallback extends ConsumerWidget {
  const _PasswordFallback();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.listen(tillStateProvider, (previous, next) {
      if (next.value?.stage == TillStage.signedIn && context.mounted) {
        Navigator.of(context).pop();
      }
    });
    return const PasswordScreen();
  }
}
