/// Checking the separate manager code while a till has no server connection.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

import 'database.dart';
import 'pin_lockout.dart';

class OfflineApprovalResult {
  const OfflineApprovalResult.accepted(this.staff)
    : reason = null,
      lockedFor = null;

  const OfflineApprovalResult.refused(this.reason, {this.lockedFor})
    : staff = null;

  final CachedApprovalStaff? staff;
  final String? reason;
  final Duration? lockedFor;

  bool get accepted => staff != null;
}

class OfflineApprovalService {
  const OfflineApprovalService({required this.database, required this.lockout});

  final OutboxDatabase database;
  final PinLockout lockout;

  Future<OfflineApprovalResult> approve({
    required String username,
    required String code,
  }) async {
    final normalizedUsername = username.trim();
    final state = await lockout.check(normalizedUsername);
    if (state.isLocked) {
      await lockout.recordLockedOutAttempt(normalizedUsername);
      return OfflineApprovalResult.refused(
        OfflineRefusalReason.lockedOut,
        lockedFor: state.remaining,
      );
    }

    final staff = await database.cachedApprovalStaff(normalizedUsername);
    if (staff == null) {
      await lockout.recordFailure(
        normalizedUsername,
        reason: OfflineRefusalReason.unknownUser,
      );
      return const OfflineApprovalResult.refused(
        OfflineRefusalReason.unknownUser,
      );
    }
    if (!staff.isActive || (staff.role != 'MANAGER' && staff.role != 'OWNER')) {
      await lockout.recordFailure(
        normalizedUsername,
        reason: OfflineRefusalReason.insufficientRole,
      );
      return const OfflineApprovalResult.refused(
        OfflineRefusalReason.insufficientRole,
      );
    }
    if (staff.verifier.isEmpty || !_matchesVerifier(code, staff.verifier)) {
      await lockout.recordFailure(normalizedUsername);
      return const OfflineApprovalResult.refused(
        OfflineRefusalReason.badCredential,
      );
    }

    await lockout.clearFailures(normalizedUsername);
    return OfflineApprovalResult.accepted(staff);
  }

  static bool _matchesVerifier(String code, String expectedHex) {
    if (code.isEmpty || expectedHex.length != 64) return false;
    final actual = sha256.convert(utf8.encode(code)).toString();
    var difference = 0;
    for (var index = 0; index < expectedHex.length; index++) {
      difference |= actual.codeUnitAt(index) ^ expectedHex.codeUnitAt(index);
    }
    return difference == 0;
  }
}
