import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../providers.dart';
import 'widgets.dart';

/// Full sign-in with a password.
///
/// Reached in two situations: registering a till for the first time, and any
/// time someone needs to sign in properly rather than switching in at the
/// counter. After a successful sign-in by a manager or owner, this offers to
/// register the device - which is what turns on PIN sign-in for everyone else.
class PasswordScreen extends ConsumerStatefulWidget {
  const PasswordScreen({super.key});

  @override
  ConsumerState<PasswordScreen> createState() => _PasswordScreenState();
}

class _PasswordScreenState extends ConsumerState<PasswordScreen> {
  final _formKey = GlobalKey<FormState>();
  final _username = TextEditingController();
  final _password = TextEditingController();

  bool _busy = false;
  bool _obscured = true;
  String? _error;

  @override
  void dispose() {
    _username.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _signIn() async {
    if (!_formKey.currentState!.validate()) return;
    FocusScope.of(context).unfocus();

    setState(() {
      _busy = true;
      _error = null;
    });

    final slug = await ref.read(sessionStoreProvider).tenantSlug();

    try {
      final session = await ref
          .read(authRepositoryProvider)
          .signInWithPassword(
            tenantSlug: slug!,
            username: _username.text.trim(),
            password: _password.text,
          );

      // Registering needs a manager, so only offer it to one. A cashier
      // signing in on an unregistered till simply gets in; someone senior can
      // set the device up later.
      final alreadyRegistered =
          await ref.read(sessionStoreProvider).isDeviceRegistered();
      if (!alreadyRegistered && session.canManage && mounted) {
        await _offerToRegister();
      }

      await ref.read(tillStateProvider.notifier).refresh();
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _offerToRegister() async {
    final register = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Set this device up as a till?'),
        content: const Text(
          'Staff will then be able to sign in here with a 4-digit PIN instead '
          'of a password, which is much quicker between customers.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Not now'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
              minimumSize: const Size(120, TillTheme.minTapTarget),
            ),
            child: const Text('Set up'),
          ),
        ],
      ),
    );

    if (register != true || !mounted) return;

    try {
      await ref.read(authRepositoryProvider).registerDevice('Till');
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('This till is ready for PIN sign-in.')),
        );
      }
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final tenantName = ref.watch(tillStateProvider).value?.tenantName;

    return Scaffold(
      appBar: AppBar(
        title: Text(tenantName?.isNotEmpty == true ? tenantName! : 'Sign in'),
        actions: [
          IconButton(
            tooltip: 'Change business',
            icon: const Icon(Icons.swap_horiz),
            onPressed: _busy ? null : _changeBusiness,
          ),
        ],
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 8),
                Text('Sign in', style: text.headlineSmall),
                const SizedBox(height: 24),
                if (_error != null) ...[
                  ErrorBanner(message: _error!),
                  const SizedBox(height: 16),
                ],
                TextFormField(
                  controller: _username,
                  autofocus: true,
                  autocorrect: false,
                  textCapitalization: TextCapitalization.none,
                  decoration: const InputDecoration(
                    labelText: 'Username',
                    prefixIcon: Icon(Icons.person_outline),
                  ),
                  validator: (value) =>
                      (value ?? '').trim().isEmpty ? 'Enter your username' : null,
                ),
                const SizedBox(height: 16),
                TextFormField(
                  controller: _password,
                  obscureText: _obscured,
                  decoration: InputDecoration(
                    labelText: 'Password',
                    prefixIcon: const Icon(Icons.lock_outline),
                    suffixIcon: IconButton(
                      // A big hit area, because typing a password wrong on a
                      // touchscreen is common and being able to check it is
                      // faster than a failed sign-in.
                      iconSize: 28,
                      icon: Icon(
                        _obscured ? Icons.visibility_outlined : Icons.visibility_off_outlined,
                      ),
                      onPressed: () => setState(() => _obscured = !_obscured),
                    ),
                  ),
                  validator: (value) =>
                      (value ?? '').isEmpty ? 'Enter your password' : null,
                  onFieldSubmitted: (_) => _signIn(),
                ),
                const SizedBox(height: 32),
                FilledButton(
                  onPressed: _busy ? null : _signIn,
                  child: _busy
                      ? const SizedBox(
                          height: 24,
                          width: 24,
                          child: CircularProgressIndicator(
                            strokeWidth: 3,
                            color: Colors.white,
                          ),
                        )
                      : const Text('Sign in'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _changeBusiness() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Change business?'),
        content: const Text(
          'This till will forget its business and its setup. You will need the '
          'business ID and a manager password to set it up again.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
              backgroundColor: TillTheme.danger,
              minimumSize: const Size(120, TillTheme.minTapTarget),
            ),
            child: const Text('Change'),
          ),
        ],
      ),
    );

    if (confirmed != true) return;
    await ref.read(sessionStoreProvider).clearEverything();
    await ref.read(tillStateProvider.notifier).refresh();
  }
}
