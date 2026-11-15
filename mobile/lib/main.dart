import 'dart:io';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/theme.dart';
import 'features/auth/business_screen.dart';
import 'features/auth/password_screen.dart';
import 'features/auth/pin_screen.dart';
import 'features/catalog/catalog_screen.dart';
import 'providers.dart';
import 'data/outbox/database.dart';
import 'data/outbox/recovery_files.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    if (await OutboxDatabase.needsFirstRunChoice()) {
      runApp(const _RecoveryGate(allowFreshStart: true));
      return;
    }
    await OutboxDatabase.initializeEncryptedStorage();
    runApp(const ProviderScope(child: TillApp()));
  } catch (_) {
    // Never start with a fresh database after a key or migration error. That
    // would make pending sales appear to have vanished.
    runApp(const _RecoveryGate(allowFreshStart: false));
  }
}

class _RecoveryGate extends StatefulWidget {
  const _RecoveryGate({required this.allowFreshStart});

  final bool allowFreshStart;

  @override
  State<_RecoveryGate> createState() => _RecoveryGateState();
}

class _RecoveryGateState extends State<_RecoveryGate> {
  bool _busy = false;
  String? _problem;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Till data recovery',
    theme: TillTheme.build(),
    home: Scaffold(
      appBar: AppBar(title: const Text('Till data recovery')),
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 520),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Icon(Icons.lock_outline, size: 56),
                const SizedBox(height: 20),
                Text(
                  widget.allowFreshStart
                      ? 'Set up a new till, or restore an encrypted backup '
                            'from another till before setup.'
                      : 'This till could not safely open its sales database. '
                            'Do not uninstall the app or clear its data. You can '
                            'restore a verified encrypted backup below.',
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 12),
                const Text(
                  'A recovery backup does not include sales made after it was '
                  'created. Keep the recovery key separate from the backup.',
                  textAlign: TextAlign.center,
                ),
                if (_problem != null) ...[
                  const SizedBox(height: 16),
                  Text(_problem!, style: TextStyle(color: TillTheme.danger)),
                ],
                if (_busy) ...[
                  const SizedBox(height: 24),
                  const Center(child: CircularProgressIndicator()),
                ] else ...[
                  const SizedBox(height: 24),
                  if (widget.allowFreshStart)
                    FilledButton(
                      onPressed: _startFreshTill,
                      child: const Text('Set up a new till'),
                    ),
                  OutlinedButton.icon(
                    onPressed: _restore,
                    icon: const Icon(Icons.restore),
                    label: const Text('Restore an encrypted backup'),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    ),
  );

  Future<void> _startFreshTill() async {
    setState(() {
      _busy = true;
      _problem = null;
    });
    try {
      await OutboxDatabase.initializeEncryptedStorage();
      runApp(const ProviderScope(child: TillApp()));
    } catch (error) {
      if (mounted) {
        setState(() => _problem = 'Could not set up this till: $error');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _restore() async {
    setState(() {
      _busy = true;
      _problem = null;
    });
    String? importedPath;
    try {
      importedPath = await RecoveryFiles.importEncryptedBackup();
      if (importedPath == null) return;
      if (!mounted) return;
      final key = await _askForRecoveryKey();
      if (key == null) return;

      await OutboxDatabase.restoreEncryptedBackup(File(importedPath), key);
      await OutboxDatabase.initializeEncryptedStorage();
      runApp(const ProviderScope(child: TillApp()));
    } catch (error) {
      if (mounted) {
        setState(
          () => _problem =
              'The backup was not restored. The till data was left unchanged. $error',
        );
      }
    } finally {
      if (importedPath != null) {
        final imported = File(importedPath);
        try {
          if (await imported.exists()) await imported.delete();
        } on FileSystemException {
          // Keep the error UI useful even if the OS refuses cache cleanup.
        }
      }
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<String?> _askForRecoveryKey() async {
    final controller = TextEditingController();
    var obscure = true;
    var invalid = false;
    final key = await showDialog<String>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('Enter the till recovery key'),
          content: TextField(
            controller: controller,
            autofocus: true,
            autocorrect: false,
            enableSuggestions: false,
            obscureText: obscure,
            decoration: InputDecoration(
              labelText: '64-character recovery key',
              errorText: invalid ? 'Enter the full recovery key.' : null,
              suffixIcon: IconButton(
                tooltip: obscure ? 'Show key' : 'Hide key',
                icon: Icon(obscure ? Icons.visibility : Icons.visibility_off),
                onPressed: () => setDialogState(() => obscure = !obscure),
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () {
                final value = controller.text.trim();
                if (OutboxDatabase.isValidRecoveryKey(value)) {
                  Navigator.pop(context, value);
                } else {
                  setDialogState(() => invalid = true);
                }
              },
              child: const Text('Verify and restore'),
            ),
          ],
        ),
      ),
    );
    controller.dispose();
    return key;
  }
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
