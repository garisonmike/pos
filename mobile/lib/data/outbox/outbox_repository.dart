/// Queueing sales the till could not send, and sending them when it can.
///
/// The rule this whole file serves: **a queued sale is deleted only on a
/// verdict from the server, never on a hopeful assumption that the upload
/// worked.** A request that hangs for ninety seconds and succeeded invisibly is
/// the normal failure on Kenyan mobile data, not an edge case, so "I sent it"
/// is not evidence that it arrived.
///
/// Which is also why `clientUuid` is generated once, when the sale is rung up,
/// and never regenerated on retry. Regenerating it is precisely how one sale
/// becomes two.
library;

import 'dart:convert';

import 'package:drift/drift.dart';
import 'package:uuid/uuid.dart';

import '../../core/api_client.dart';
import '../cart/pricing.dart';
import 'database.dart';
import 'pin_lockout.dart';

/// How many queued sales go up in one request.
///
/// Matches the server's `MAX_SALES_PER_BATCH`. A till with a bigger backlog
/// sends several requests, each independently safe to retry.
const int kMaxSalesPerBatch = 200;

/// What a sync attempt did, in the terms the cashier needs.
class SyncReport {
  const SyncReport({
    this.accepted = 0,
    this.duplicate = 0,
    this.rejected = 0,
    this.refusalsSent = 0,
    this.failure,
  });

  final int accepted;
  final int duplicate;
  final int rejected;
  final int refusalsSent;

  /// Set when the batch never reached the server at all. Distinct from
  /// `rejected`, which means it arrived and was refused - one is worth
  /// retrying unchanged and the other is not.
  final ApiException? failure;

  bool get reachedServer => failure == null;
  int get settled => accepted + duplicate;

  /// Whether anything needs a person. Rejections are money that was taken and
  /// has no home, so they are never cleared away quietly.
  bool get needsAttention => rejected > 0;
}

/// The one thing the outbox needs from the network.
///
/// Narrower than [ApiClient] on purpose. The repository's job is deciding what
/// may be deleted and what must be kept, and that logic is worth testing
/// without a Dio, a token refresh and a platform keystore standing behind it.
abstract class SyncTransport {
  Future<Map<String, dynamic>> postBatch(Map<String, dynamic> body);
}

/// [SyncTransport] over the real API.
class ApiSyncTransport implements SyncTransport {
  const ApiSyncTransport(this._api);

  final ApiClient _api;

  @override
  Future<Map<String, dynamic>> postBatch(Map<String, dynamic> body) async {
    final result = await _api.post('/api/v1/sync/sales/', body: body);
    return result.asMap;
  }
}

class OutboxRepository {
  OutboxRepository({
    required OutboxDatabase database,
    required SyncTransport transport,
    required PinLockout lockout,
    Uuid? uuid,
  })  : _db = database,
        _transport = transport,
        _lockout = lockout,
        _uuid = uuid ?? const Uuid();

  final OutboxDatabase _db;
  final SyncTransport _transport;
  final PinLockout _lockout;
  final Uuid _uuid;

  /// Put a completed sale in the outbox.
  ///
  /// Called for every offline sale, and for an online one whose request failed
  /// in a way that leaves it unclear whether the server took it. The cash is
  /// already in the drawer by this point; this is the record of it.
  Future<QueuedSale> enqueue({
    required CartTotals totals,
    required int tenderedCents,
    String? storeId,
    String customerPhone = '',
    String note = '',
    OfflineApproval? approval,
    String? clientUuid,
    DateTime? at,
  }) async {
    final now = at ?? DateTime.now();
    final id = clientUuid ?? _uuid.v4();
    final sequence = await _nextDeviceSequence();

    final payload = <String, dynamic>{
      'client_uuid': id,
      'device_sequence': sequence,
      'device_created_at': now.toUtc().toIso8601String(),
      'lines': [
        for (final line in totals.lines)
          {
            'item_id': line.line.itemId,
            // Sent as a decimal string, not a double: the server's quantity
            // field has three decimal places and a double would occasionally
            // arrive as 0.3330000000000001.
            'quantity': (line.line.quantityMilli / 1000).toStringAsFixed(3),
            if (line.line.discountBps > 0) 'discount_bps': line.line.discountBps,
            if (line.line.discountCents > 0) 'discount_cents': line.line.discountCents,
          },
      ],
      'tendered_cents': tenderedCents,
      'round_to_shilling': true,
      // Sent so the server can flag a disagreement. It is never trusted - the
      // server prices the cart again from its own catalogue.
      'total_cents': totals.totalCents,
      if (storeId != null) 'store_id': storeId,
      if (customerPhone.isNotEmpty) 'customer_phone': customerPhone,
      if (note.isNotEmpty) 'note': note,
      if (approval != null) 'discount_authorization': approval.toJson(),
    };

    await _db.into(_db.queuedSales).insert(
          QueuedSalesCompanion.insert(
            clientUuid: id,
            deviceSequence: sequence,
            payloadJson: jsonEncode(payload),
            deviceCreatedAt: now,
          ),
        );

    return (await _byUuid(id))!;
  }

  /// Sales still waiting on a verdict, oldest first.
  ///
  /// Oldest first because a shop reads its day in order, and because the
  /// receipt numbers the server allocates will then follow the order the sales
  /// actually happened in.
  Future<List<QueuedSale>> pending() {
    return (_db.select(_db.queuedSales)
          ..where((t) => t.status.equals(SyncStatus.pending))
          ..orderBy([(t) => OrderingTerm.asc(t.deviceSequence)]))
        .get();
  }

  /// Sales the server refused. These need a person, not a retry.
  Future<List<QueuedSale>> rejected() {
    return (_db.select(_db.queuedSales)
          ..where((t) => t.status.equals(SyncStatus.rejected))
          ..orderBy([(t) => OrderingTerm.asc(t.deviceSequence)]))
        .get();
  }

  Future<int> pendingCount() async => (await pending()).length;

  /// Send one batch and apply the verdicts.
  ///
  /// Returns a report rather than throwing, because a failed sync is an
  /// ordinary state for this app rather than an error: the till goes on
  /// selling and tries again later.
  Future<SyncReport> sync({required String deviceId}) async {
    final queued = await pending();
    final refusals = await _lockout.pendingTelemetry();

    if (queued.isEmpty && refusals.isEmpty) {
      return const SyncReport();
    }

    final batch = queued.take(kMaxSalesPerBatch).toList();
    final body = <String, dynamic>{
      'device_id': deviceId,
      'sales': [
        for (final sale in batch)
          jsonDecode(sale.payloadJson) as Map<String, dynamic>,
      ],
      'refused_authorizations': refusals,
    };

    await _markAttempted(batch);

    Map<String, dynamic> response;
    try {
      response = await _transport.postBatch(body);
    } on ApiException catch (error) {
      // Nothing is deleted. The batch may or may not have landed, and the
      // client_uuid on each sale is what makes finding out safe later.
      return SyncReport(failure: error);
    }

    final verdicts = (response['results'] as List<dynamic>? ?? const []);

    var accepted = 0, duplicate = 0, rejectedCount = 0;
    for (final raw in verdicts) {
      final verdict = (raw as Map).cast<String, dynamic>();
      final status = verdict['status'] as String;

      switch (status) {
        case SyncStatus.accepted:
          accepted++;
        case SyncStatus.duplicate:
          duplicate++;
        case SyncStatus.rejected:
          rejectedCount++;
      }
      await _applyVerdict(verdict);
    }

    // Only now, once the server has answered, is the local telemetry dropped.
    // Clearing it optimistically would lose the entries on exactly the flaky
    // connection this whole subsystem exists for.
    if (refusals.isNotEmpty) {
      await _lockout.clearTelemetry();
    }

    return SyncReport(
      accepted: accepted,
      duplicate: duplicate,
      rejected: rejectedCount,
      refusalsSent: response['refusals_recorded'] as int? ?? 0,
    );
  }

  /// Apply one sale's verdict.
  ///
  /// `accepted` and `duplicate` both mean the server has it, so both are safe
  /// to clear - a replay is a success, and treating it as a failure would leave
  /// rows here forever. `rejected` is kept: it is money that was taken and has
  /// no home, and quietly discarding it would be the worst thing this app could
  /// do.
  Future<void> _applyVerdict(Map<String, dynamic> verdict) async {
    final id = verdict['client_uuid'] as String;
    final status = verdict['status'] as String;

    if (status == SyncStatus.accepted || status == SyncStatus.duplicate) {
      await (_db.update(_db.queuedSales)..where((t) => t.clientUuid.equals(id)))
          .write(
        QueuedSalesCompanion(
          status: Value(status),
          serverSaleId: Value(verdict['sale_id'] as String?),
          receiptNumber: Value(verdict['receipt_number'] as int?),
        ),
      );
      // Settled rows are removed rather than kept as history. The server is
      // the record of a sale that reached it; keeping a second copy here would
      // grow without bound on a device with no housekeeping.
      await (_db.delete(_db.queuedSales)
            ..where((t) => t.clientUuid.equals(id)))
          .go();
      return;
    }

    await (_db.update(_db.queuedSales)..where((t) => t.clientUuid.equals(id)))
        .write(
      QueuedSalesCompanion(
        status: const Value(SyncStatus.rejected),
        failureCode: Value(verdict['code'] as String?),
        failureDetail: Value(verdict['detail'] as String?),
      ),
    );
  }

  Future<void> _markAttempted(List<QueuedSale> batch) async {
    final now = DateTime.now();
    for (final sale in batch) {
      await (_db.update(_db.queuedSales)
            ..where((t) => t.clientUuid.equals(sale.clientUuid)))
          .write(
        QueuedSalesCompanion(
          attempts: Value(sale.attempts + 1),
          lastAttemptAt: Value(now),
        ),
      );
    }
  }

  /// The next number in this till's own series.
  ///
  /// Monotonic and never reused, so a gap is visible to the shop - which is how
  /// a wiped tablet or a hand-edited database gets noticed. Derived from the
  /// highest ever issued rather than from a row count, because deleting settled
  /// rows would otherwise make the series repeat itself.
  Future<int> _nextDeviceSequence() async {
    final row = await (_db.selectOnly(_db.queuedSales)
          ..addColumns([_db.queuedSales.deviceSequence.max()]))
        .getSingleOrNull();
    final highest = row?.read(_db.queuedSales.deviceSequence.max()) ?? 0;

    final cursor = await (_db.select(_db.syncCursor)
          ..where((t) => t.name.equals(_sequenceCursor)))
        .getSingleOrNull();
    final watermark = int.tryParse(cursor?.serverTime ?? '') ?? 0;

    final next = (highest > watermark ? highest : watermark) + 1;
    await _db.into(_db.syncCursor).insertOnConflictUpdate(
          SyncCursorCompanion.insert(
            name: _sequenceCursor,
            serverTime: Value(next.toString()),
          ),
        );
    return next;
  }

  static const _sequenceCursor = 'device_sequence';

  Future<QueuedSale?> _byUuid(String id) =>
      (_db.select(_db.queuedSales)..where((t) => t.clientUuid.equals(id)))
          .getSingleOrNull();
}

/// A discount the till approved with no connection.
///
/// Carries no PIN and no password. The device has already done the check
/// against its cached copy, and sending the raw credential afterwards would
/// turn a replayed batch into a bundle of manager PINs in flight.
class OfflineApproval {
  const OfflineApproval({
    required this.username,
    required this.reason,
    required this.pinVersion,
    required this.authorizedAt,
  });

  final String username;
  final String reason;

  /// Which version of that manager's cached PIN was checked. A counter, not a
  /// digest - it carries nothing derived from the PIN. The server compares it
  /// to the version it holds now, which is what catches a till approving
  /// against a PIN that has since been changed or revoked.
  final int pinVersion;

  final DateTime authorizedAt;

  Map<String, dynamic> toJson() => {
        'username': username,
        'reason': reason,
        'pin_version': pinVersion,
        'authorized_at': authorizedAt.toUtc().toIso8601String(),
      };
}
