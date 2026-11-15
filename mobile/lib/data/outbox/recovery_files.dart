import 'package:flutter/services.dart';

/// Android's Storage Access Framework keeps backup destinations client-owned.
/// The app writes the already-encrypted SQLite file directly to the location
/// selected by the owner; it does not upload it to a vendor service itself.
class RecoveryFiles {
  RecoveryFiles._();

  static const _channel = MethodChannel('ke.pos.pos_till/recovery_files');

  static Future<bool> exportEncryptedBackup({
    required String sourcePath,
    required String suggestedName,
  }) async {
    return await _channel.invokeMethod<bool>('exportEncryptedBackup', {
          'sourcePath': sourcePath,
          'suggestedName': suggestedName,
        }) ??
        false;
  }

  /// Copies a selected provider document into the app's private cache. The
  /// caller must delete the returned temporary file after validation.
  static Future<String?> importEncryptedBackup() =>
      _channel.invokeMethod<String>('importEncryptedBackup');
}
