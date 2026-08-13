import '../core/api_client.dart';
import '../core/session_store.dart';
import 'models.dart';

/// Signing in, and the two-step setup a till goes through before it can.
///
/// A till's life has three states, and the router keys off them:
///
/// 1. **Unclaimed** - no business slug stored. Someone must say which shop this
///    is.
/// 2. **Registered** - a device token is stored, so cashiers can sign in with a
///    PIN alone.
/// 3. **Signed in** - a cashier holds tokens.
///
/// Getting from 1 to 2 needs a manager's password once. From then on the
/// counter runs on four digits, which is the whole point: a password typed on a
/// tablet between customers is friction, and friction is how a shop ends up
/// with one shared login and no idea who took the money.
class AuthRepository {
  AuthRepository({required ApiClient api, required SessionStore session})
    : _api = api,
      _session = session;

  final ApiClient _api;
  final SessionStore _session;

  /// Full sign-in with a password. Also used to claim an unregistered till.
  Future<Session> signInWithPassword({
    required String tenantSlug,
    required String username,
    required String password,
  }) async {
    final result = await _api.post(
      '/api/v1/auth/login/',
      skipAuth: true,
      body: {
        'tenant_slug': tenantSlug,
        'username': username,
        'password': password,
      },
    );

    return _storeSession(result.asMap, tenantSlug: tenantSlug, username: username);
  }

  /// Fast sign-in for a cashier taking over the counter.
  ///
  /// Sends the stored device token alongside the PIN. Without a registered till
  /// this cannot succeed, which is what makes four digits an acceptable secret.
  Future<Session> signInWithPin({
    required String username,
    required String pin,
  }) async {
    final slug = await _session.tenantSlug();
    final deviceToken = await _session.deviceToken();

    if (slug == null || deviceToken == null) {
      throw const ApiException(
        status: 0,
        code: 'device_not_registered',
        message: 'This till has not been set up yet.',
      );
    }

    final result = await _api.post(
      '/api/v1/auth/pin-login/',
      skipAuth: true,
      body: {
        'tenant_slug': slug,
        'device_token': deviceToken,
        'username': username,
        'pin': pin,
      },
    );

    return _storeSession(result.asMap, tenantSlug: slug, username: username);
  }

  /// Register this device, so PIN sign-in works from it afterwards.
  ///
  /// Requires a manager or owner to be signed in. The token comes back exactly
  /// once and is written straight to the keystore; it is never held in state
  /// where a screen could log or display it.
  Future<void> registerDevice(String name) async {
    final result = await _api.post('/api/v1/auth/devices/', body: {'name': name});
    await _session.saveDeviceToken(result.asMap['device_token'] as String);
  }

  /// Who is signed in, according to the server.
  Future<Session> currentSession() async {
    final result = await _api.get('/api/v1/auth/me/');
    final body = result.asMap;
    final session = Session.fromJson(body);
    await _session.saveTenant(slug: session.tenantSlug, name: session.tenantName);
    return session;
  }

  /// Sign the person out, leaving the till registered for the next cashier.
  Future<void> signOut() async {
    final refresh = await _session.refreshToken();
    if (refresh != null) {
      try {
        await _api.post('/api/v1/auth/logout/', body: {'refresh': refresh});
      } on ApiException {
        // The token may already be spent or the network may be down. Neither
        // should keep someone signed in on the device in front of them - the
        // local clear below is what actually ends the session here.
      }
    }
    await _session.clear();
  }

  Future<Session> _storeSession(
    Map<String, dynamic> body, {
    required String tenantSlug,
    required String username,
  }) async {
    await _session.saveTokens(
      access: body['access'] as String,
      refresh: body['refresh'] as String,
    );
    await _session.saveLastUsername(username);

    final session = Session.fromJson(body);
    await _session.saveTenant(
      slug: tenantSlug,
      name: session.tenantName.isEmpty ? null : session.tenantName,
    );
    return session;
  }
}
