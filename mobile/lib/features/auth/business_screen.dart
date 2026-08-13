import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../providers.dart';

/// The very first screen: which shop is this till for.
///
/// Asked once, then remembered forever. This is the price of usernames being
/// unique per business rather than across the platform - and it is worth
/// paying, because the alternative is a shop discovering at sign-up that
/// strangers have taken its staff names.
class BusinessScreen extends ConsumerStatefulWidget {
  const BusinessScreen({super.key});

  @override
  ConsumerState<BusinessScreen> createState() => _BusinessScreenState();
}

class _BusinessScreenState extends ConsumerState<BusinessScreen> {
  final _controller = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _saving = false;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _continue() async {
    if (!_formKey.currentState!.validate()) return;

    setState(() => _saving = true);
    await ref
        .read(sessionStoreProvider)
        .saveTenant(slug: _controller.text.trim().toLowerCase());
    await ref.read(tillStateProvider.notifier).refresh();
    if (mounted) setState(() => _saving = false);
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;

    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 32),
                Icon(
                  Icons.storefront_outlined,
                  size: 64,
                  color: Theme.of(context).colorScheme.primary,
                ),
                const SizedBox(height: 24),
                Text('Set up this till', style: text.headlineSmall),
                const SizedBox(height: 8),
                Text(
                  'Enter the business ID you were given. You only need to do '
                  'this once on this device.',
                  style: text.bodyLarge?.copyWith(color: TillTheme.muted),
                ),
                const SizedBox(height: 32),
                TextFormField(
                  controller: _controller,
                  autofocus: true,
                  textCapitalization: TextCapitalization.none,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'Business ID',
                    hintText: 'mama-njeri-duka',
                    prefixIcon: Icon(Icons.badge_outlined),
                  ),
                  validator: (value) {
                    final slug = (value ?? '').trim();
                    if (slug.isEmpty) return 'Enter the business ID';
                    if (!RegExp(r'^[a-z0-9-]+$').hasMatch(slug.toLowerCase())) {
                      return 'Letters, numbers and dashes only';
                    }
                    return null;
                  },
                  onFieldSubmitted: (_) => _continue(),
                ),
                const Spacer(),
                FilledButton(
                  onPressed: _saving ? null : _continue,
                  child: _saving
                      ? const SizedBox(
                          height: 24,
                          width: 24,
                          child: CircularProgressIndicator(
                            strokeWidth: 3,
                            color: Colors.white,
                          ),
                        )
                      : const Text('Continue'),
                ),
                const SizedBox(height: 16),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
