import 'package:material_ui/material_ui.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../data/outbox/database.dart';
import '../../data/outbox/recovery_files.dart';
import '../../providers.dart';

/// Owner-controlled export of the encrypted till database and its recovery key.
class RecoveryScreen extends ConsumerStatefulWidget {
  const RecoveryScreen({super.key});

  @override
  ConsumerState<RecoveryScreen> createState() => _RecoveryScreenState();
}

class _RecoveryScreenState extends ConsumerState<RecoveryScreen> {
  bool _loading = true;
  bool _keySaved = false;
  bool _busy = false;
  DateTime? _lastBackup;
  String? _problem;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    final keySaved = await OutboxDatabase.recoveryKeyAcknowledged();
    final lastBackup = await OutboxDatabase.lastRecoveryBackupAt();
    if (!mounted) return;
    setState(() {
      _keySaved = keySaved;
      _lastBackup = lastBackup;
      _loading = false;
    });
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Till data recovery')),
    body: _loading
        ? const Center(child: CircularProgressIndicator())
        : ListView(
            padding: const EdgeInsets.all(20),
            children: [
              const Icon(Icons.enhanced_encryption, size: 42),
              const SizedBox(height: 16),
              Text(
                'Protect sales that have not synced yet',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 8),
              const Text(
                'The backup is an encrypted copy of this till database. Save '
                'it to a client-controlled location such as the business’s '
                'Google Drive. Keep the recovery key separately in a '
                'password manager or another secure place.',
              ),
              const SizedBox(height: 8),
              const Text(
                'A backup only contains sales recorded up to its creation '
                'time. Make a new backup after offline trading and whenever '
                'the previous backup is older than the unsynced sales.',
                style: TextStyle(fontWeight: FontWeight.w600),
              ),
              if (_problem != null) ...[
                const SizedBox(height: 12),
                Text(_problem!, style: TextStyle(color: TillTheme.danger)),
              ],
              const SizedBox(height: 20),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        _keySaved
                            ? 'Recovery key saved separately'
                            : 'Set up recovery key',
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        _keySaved
                            ? 'The till key is ready to recover an exported backup.'
                            : 'Record the key outside this till before making a backup.',
                      ),
                      const SizedBox(height: 12),
                      if (_keySaved)
                        OutlinedButton.icon(
                          onPressed: _showRecoveryKey,
                          icon: const Icon(Icons.key),
                          label: const Text('View recovery key'),
                        )
                      else
                        FilledButton.icon(
                          onPressed: _showRecoveryKey,
                          icon: const Icon(Icons.key),
                          label: const Text('Show and save recovery key'),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        'Encrypted backup',
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        _lastBackup == null
                            ? 'No backup has been created from this till.'
                            : 'Last saved: ${_lastBackup!.toLocal()}',
                      ),
                      const SizedBox(height: 12),
                      FilledButton.icon(
                        onPressed: _busy || !_keySaved ? null : _exportBackup,
                        icon: const Icon(Icons.upload_file),
                        label: Text(
                          _busy ? 'Saving backup…' : 'Save encrypted backup',
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              if (_busy) ...[
                const SizedBox(height: 16),
                const Center(child: CircularProgressIndicator()),
              ],
            ],
          ),
  );

  Future<void> _showRecoveryKey() async {
    try {
      final key = await OutboxDatabase.recoveryKey();
      if (!mounted) return;
      final accepted = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Store this key separately'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                  'Anyone with both this key and a backup can read the till '
                  'data. Keep the key separate from the backup file.',
                ),
                const SizedBox(height: 16),
                SelectableText(
                  key,
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 15,
                    letterSpacing: 1,
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Close'),
            ),
            if (!_keySaved)
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('I saved it separately'),
              ),
          ],
        ),
      );
      if (accepted == true) {
        await OutboxDatabase.acknowledgeRecoveryKey();
        await _refresh();
      }
    } catch (error) {
      _showMessage('Could not read the recovery key: $error');
    }
  }

  Future<void> _exportBackup() async {
    setState(() {
      _busy = true;
      _problem = null;
    });
    final database = ref.read(outboxDatabaseProvider);
    try {
      final key = await OutboxDatabase.recoveryKey();
      if (!OutboxDatabase.isValidRecoveryKey(key)) {
        throw StateError('The recovery key is not valid.');
      }

      // Closing Drift checkpoints SQLite's write-ahead log so this single
      // encrypted file contains the latest committed outbox rows.
      await database.closeSafely();
      final file = await OutboxDatabase.databaseFile();
      final businessId =
          ref
              .read(tillStateProvider)
              .value
              ?.tenantSlug
              ?.replaceAll(RegExp(r'[^a-zA-Z0-9_-]'), '-') ??
          'business';
      final stamp = DateTime.now()
          .toUtc()
          .toIso8601String()
          .replaceAll('-', '')
          .replaceAll(':', '')
          .replaceAll('.', '');
      final saved = await RecoveryFiles.exportEncryptedBackup(
        sourcePath: file.path,
        suggestedName: 'pos-till-$businessId-recovery-$stamp.sqlite3',
      );
      if (saved) {
        await OutboxDatabase.recordRecoveryBackup();
        await _refresh();
        _showMessage('Encrypted backup saved. Keep its recovery key separate.');
      }
    } catch (error) {
      _showMessage('Backup was not saved: $error');
    } finally {
      // A cancelled file picker still leaves the provider closed.
      ref.invalidate(outboxDatabaseProvider);
      if (mounted) setState(() => _busy = false);
    }
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(message)));
  }
}
