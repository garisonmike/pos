/// Fetching reports and running a drawer.
///
/// **Reports are online-only, and that is a design decision rather than a
/// missing feature.** The outbox exists for money that must not be lost; a
/// report is regenerable. Caching aggregates on the device would show a manager
/// stale figures with no indication they were stale, and a wrong number that
/// looks current is worse than no number at all - so a request that cannot
/// reach the server produces [ReportsUnavailable], which the screen renders as
/// its own state.
///
/// Shifts are different. Opening and closing a drawer is a *money* action, so
/// its failures surface as ordinary errors a person can act on rather than
/// being quietly swallowed.
library;

import '../../core/api_client.dart';
import 'models.dart';

/// The report endpoints, narrowed to what this repository needs.
///
/// The same seam the outbox and checkout use: the decisions worth testing here
/// are about *unavailability*, and they should not need a Dio and a platform
/// keystore standing behind them.
abstract class ReportsTransport {
  Future<Map<String, dynamic>> get(String path, {Map<String, dynamic>? query});
  Future<Map<String, dynamic>> post(String path, {Map<String, dynamic>? body});
}

class ApiReportsTransport implements ReportsTransport {
  const ApiReportsTransport(this._api);

  final ApiClient _api;

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, dynamic>? query}) async {
    final result = await _api.get(path, query: query);
    return result.asMap;
  }

  @override
  Future<Map<String, dynamic>> post(String path, {Map<String, dynamic>? body}) async {
    final result = await _api.post(path, body: body);
    return result.asMap;
  }
}

/// Raised when a report could not be fetched because the server was unreachable.
///
/// Deliberately its own type rather than a null or an empty list. A screen that
/// received an empty report would render zeroes, and a manager would read those
/// zeroes as a quiet day rather than as a missing connection.
class ReportsUnavailable implements Exception {
  const ReportsUnavailable(this.cause);

  final ApiException cause;

  bool get isOffline => cause.isOffline;

  String get message => isOffline
      ? 'No connection, so reports cannot be shown.'
      : 'Reports could not be loaded.';
}

class ReportsRepository {
  ReportsRepository(this._transport);

  final ReportsTransport _transport;

  Future<T> _read<T>(
    String path,
    T Function(Map<String, dynamic>) parse, {
    Map<String, dynamic>? query,
  }) async {
    try {
      return parse(await _transport.get(path, query: query));
    } on ApiException catch (error) {
      if (error.isOffline || error.status == 0 || error.status >= 500) {
        // Never degraded into an empty report. See ReportsUnavailable.
        throw ReportsUnavailable(error);
      }
      rethrow;
    }
  }

  /// Takings for a period.
  Future<List<SalesSummary>> sales({String granularity = 'day', String? on}) {
    return _read(
      '/api/v1/reports/sales/',
      (body) => [
        for (final row in (body['periods'] as List? ?? const []))
          SalesSummary.fromJson((row as Map).cast<String, dynamic>()),
      ],
      query: {'granularity': granularity, if (on != null) 'on': on},
    );
  }

  /// What sold, ranked either way.
  ///
  /// The order is passed through rather than sorted here: the two rankings are
  /// different questions and the server answers both, so re-sorting on the
  /// device would only be a second opinion about which one was asked.
  Future<List<BestSeller>> bestSellers({
    String granularity = 'day',
    String order = 'revenue',
  }) {
    return _read(
      '/api/v1/reports/best-sellers/',
      (body) => [
        for (final row in (body['items'] as List? ?? const []))
          BestSeller.fromJson((row as Map).cast<String, dynamic>()),
      ],
      query: {'granularity': granularity, 'order': order},
    );
  }

  /// Per-cashier figures, with the note the server attaches.
  Future<CashierReport> cashiers({String granularity = 'day'}) {
    return _read(
      '/api/v1/reports/cashiers/',
      CashierReport.fromJson,
      query: {'granularity': granularity},
    );
  }

  /// Shifts as counted, beside whatever arrived after they closed.
  Future<DrawerReport> drawers({String granularity = 'day'}) {
    return _read(
      '/api/v1/reports/drawers/',
      DrawerReport.fromJson,
      query: {'granularity': granularity},
    );
  }

  // ---- Running a drawer ------------------------------------------------

  /// The drawer this person currently has open, if any.
  Future<OpenShift?> currentShift() async {
    final body = await _read('/api/v1/shifts/current/', (json) => json);
    final shift = body['shift'];
    if (shift == null) return null;
    return OpenShift.fromJson((shift as Map).cast<String, dynamic>());
  }

  Future<OpenShift> openShift({required int openingFloatCents, String note = ''}) async {
    final body = await _transport.post(
      '/api/v1/shifts/open/',
      body: {
        'opening_float_cents': openingFloatCents,
        if (note.isNotEmpty) 'note': note,
      },
    );
    return OpenShift.fromJson(body);
  }

  /// Close a drawer with a counted figure.
  ///
  /// The count goes up and the expectation comes back. Nothing asks the server
  /// what it expected beforehand, because there is no endpoint that would
  /// answer - the blind count is enforced there, and this call is shaped by it.
  Future<ClosedShift> closeShift({
    required String shiftId,
    required int declaredCents,
    Map<int, int>? denominations,
    String note = '',
  }) async {
    final body = await _transport.post(
      '/api/v1/shifts/$shiftId/close/',
      body: {
        'declared_closing_cents': declaredCents,
        if (denominations != null && denominations.isNotEmpty)
          'denominations': {
            for (final entry in denominations.entries)
              entry.key.toString(): entry.value,
          },
        if (note.isNotEmpty) 'note': note,
      },
    );
    return ClosedShift.fromJson(body);
  }

  Future<void> recordCash({
    required String shiftId,
    required String kind,
    required int amountCents,
    required String reason,
  }) async {
    await _transport.post(
      '/api/v1/shifts/$shiftId/cash/',
      body: {'kind': kind, 'amount_cents': amountCents, 'reason': reason},
    );
  }
}
