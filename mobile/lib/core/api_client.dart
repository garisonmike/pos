import 'package:dio/dio.dart';

import 'session_store.dart';

/// The single HTTP client, and the one place tokens are attached and renewed.
///
/// Refresh lives in an interceptor rather than at call sites for a reason that
/// matters more here than in most apps: access tokens are short-lived by design
/// (signing out cannot revoke one already issued), and a till sits idle between
/// customers. So *any* screen can be the first to meet an expired token, and a
/// scheme where each remembered to refresh would fail on whichever screen the
/// author forgot.
///
/// The API returns one error shape for everything, which is what lets
/// [ApiException] be the only failure type the UI has to understand.
class ApiClient {
  ApiClient({required this.baseUrl, required SessionStore session, Dio? dio})
    : _session = session,
      _dio = dio ?? Dio() {
    _dio.options
      ..baseUrl = baseUrl
      ..connectTimeout = const Duration(seconds: 10)
      // Generous, because mobile data here is slow rather than absent. Failing
      // at three seconds would send a till into offline mode on a connection
      // that would have worked.
      ..receiveTimeout = const Duration(seconds: 20)
      ..headers['Content-Type'] = 'application/json'
      // Any status is "successful" at the transport layer; status handling
      // happens in one place below rather than through thrown DioExceptions.
      ..validateStatus = (_) => true;

    _dio.interceptors.add(
      InterceptorsWrapper(onRequest: _attachToken, onResponse: _handleResponse),
    );
  }

  final String baseUrl;
  final SessionStore _session;
  final Dio _dio;

  /// Guards against a burst of parallel refreshes.
  ///
  /// A screen that fires three requests at once would otherwise refresh three
  /// times, and with rotation enabled on the server the second and third would
  /// present a refresh token the first had already replaced - signing the
  /// cashier out mid-shift.
  Future<void>? _refreshInFlight;

  Future<void> _attachToken(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    if (options.extra['skipAuth'] != true) {
      final token = await _session.accessToken();
      if (token != null) {
        options.headers['Authorization'] = 'Bearer $token';
      }
    }
    handler.next(options);
  }

  Future<void> _handleResponse(
    Response response,
    ResponseInterceptorHandler handler,
  ) async {
    handler.next(response);
  }

  Future<ApiResult> get(String path, {Map<String, dynamic>? query}) =>
      _send(() => _dio.get(path, queryParameters: query));

  Future<ApiResult> post(
    String path, {
    Object? body,
    bool skipAuth = false,
  }) => _send(
    () => _dio.post(
      path,
      data: body,
      options: Options(extra: {'skipAuth': skipAuth}),
    ),
  );

  /// Runs a request, renewing the token once if the server says it is stale.
  ///
  /// Exactly one retry. A loop would turn a genuinely revoked session into an
  /// endless cycle of refreshes against a server that will never accept them.
  Future<ApiResult> _send(Future<Response> Function() send) async {
    Response response;
    try {
      response = await send();
    } on DioException catch (error) {
      throw ApiException.fromDio(error);
    }

    if (response.statusCode == 401 && await _session.hasRefreshToken()) {
      final renewed = await _refreshOnce();
      if (renewed) {
        try {
          response = await send();
        } on DioException catch (error) {
          throw ApiException.fromDio(error);
        }
      }
    }

    return _interpret(response);
  }

  Future<bool> _refreshOnce() async {
    // Join whichever refresh is already running rather than starting another.
    if (_refreshInFlight != null) {
      await _refreshInFlight;
      return _session.hasAccessToken();
    }

    final completer = _performRefresh();
    _refreshInFlight = completer;
    try {
      await completer;
    } finally {
      _refreshInFlight = null;
    }
    return _session.hasAccessToken();
  }

  Future<void> _performRefresh() async {
    final refresh = await _session.refreshToken();
    if (refresh == null) return;

    final response = await _dio.post(
      '/api/v1/auth/refresh/',
      data: {'refresh': refresh},
      options: Options(extra: {'skipAuth': true}),
    );

    if (response.statusCode == 200 && response.data is Map) {
      final data = response.data as Map;
      await _session.saveTokens(
        access: data['access'] as String,
        // Rotation is on server-side, so a refreshed response carries a new
        // refresh token. Keeping the old one would sign the till out at the
        // next renewal.
        refresh: data['refresh'] as String? ?? refresh,
      );
    } else {
      // The refresh token is spent or revoked. Clearing it is what makes the
      // router send the cashier back to sign-in rather than leaving them on a
      // screen that silently fails every request.
      await _session.clear();
    }
  }

  ApiResult _interpret(Response response) {
    final status = response.statusCode ?? 0;
    final data = response.data;

    if (status >= 200 && status < 300) {
      return ApiResult(status: status, data: data);
    }

    final body = data is Map<String, dynamic> ? data : <String, dynamic>{};
    throw ApiException(
      status: status,
      code: body['code'] as String? ?? 'error',
      message:
          body['detail'] as String? ??
          'Something went wrong. Please try again.',
      fields: body['fields'] as Map<String, dynamic>?,
      retryAfterSeconds: body['retry_after_seconds'] as int?,
    );
  }
}

/// A successful response.
class ApiResult {
  const ApiResult({required this.status, required this.data});

  final int status;
  final dynamic data;

  Map<String, dynamic> get asMap => (data as Map).cast<String, dynamic>();
  List<dynamic> get asList => data is List ? data as List : (data['results'] as List);
}

/// Every failure the UI has to understand, in one type.
class ApiException implements Exception {
  const ApiException({
    required this.status,
    required this.code,
    required this.message,
    this.fields,
    this.retryAfterSeconds,
  });

  factory ApiException.fromDio(DioException error) {
    final offline =
        error.type == DioExceptionType.connectionTimeout ||
        error.type == DioExceptionType.receiveTimeout ||
        error.type == DioExceptionType.connectionError;

    return ApiException(
      status: 0,
      code: offline ? 'offline' : 'network_error',
      message: offline
          ? 'No connection to the server. Check the network and try again.'
          : 'Could not reach the server.',
    );
  }

  final int status;
  final String code;
  final String message;
  final Map<String, dynamic>? fields;
  final int? retryAfterSeconds;

  /// Whether this looks like a connectivity problem rather than a refusal.
  ///
  /// Milestone 3 uses this to decide when to queue a sale locally instead of
  /// failing it.
  bool get isOffline => code == 'offline' || code == 'network_error';

  /// The business has been suspended by the platform operator.
  ///
  /// A distinct status from "not allowed" precisely so the till can say
  /// "contact your provider" rather than showing a permissions error to a
  /// cashier who has done nothing wrong.
  bool get isSuspended => status == 402;

  /// The till is locked after too many wrong PINs.
  bool get isLockedOut => code == 'pin_locked_out';

  @override
  String toString() => message;
}
