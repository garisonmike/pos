import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/theme.dart';
import 'features/auth/business_screen.dart';
import 'features/auth/password_screen.dart';
import 'features/auth/pin_screen.dart';
import 'features/catalog/catalog_screen.dart';
import 'providers.dart';

void main() {
  runApp(const ProviderScope(child: TillApp()));
}

/// The till.
///
/// Which screen shows is decided entirely by the till's stage, in one place,
/// rather than by screens pushing and popping each other. That keeps the answer
/// to "why am I looking at sign-in" to a single readable rule, and means a
/// session expiring anywhere lands the user somewhere sensible without every
/// screen having to handle it.
class TillApp extends ConsumerWidget {
  const TillApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return MaterialApp(
      title: 'Till',
      debugShowCheckedModeBanner: false,
      theme: TillTheme.build(),
      home: const _Root(),
    );
  }
}

class _Root extends ConsumerWidget {
  const _Root();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final till = ref.watch(tillStateProvider);

    return till.when(
      loading: () => const _Splash(),
      error: (error, _) => _Splash(message: error.toString()),
      data: (state) => switch (state.stage) {
        TillStage.unclaimed => const BusinessScreen(),
        // Claimed but unregistered: no device token, so PIN sign-in cannot
        // work and a password is the only way through.
        TillStage.claimed => const PasswordScreen(),
        TillStage.registered => const PinScreen(),
        TillStage.signedIn => const CatalogScreen(),
      },
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash({this.message});

  final String? message;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.storefront,
              size: 64,
              color: Theme.of(context).colorScheme.primary,
            ),
            const SizedBox(height: 24),
            if (message == null)
              const CircularProgressIndicator()
            else
              Padding(
                padding: const EdgeInsets.all(24),
                child: Text(message!, textAlign: TextAlign.center),
              ),
          ],
        ),
      ),
    );
  }
}
