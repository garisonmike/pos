import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Where the till keeps what it knows between launches.
///
/// The split between the two backing stores is deliberate. Anything that would
/// let someone act as this till goes in the platform keystore; anything that is
/// merely a preference goes in shared preferences, which is world-readable on a
/// rooted device.
///
/// The **device token** is the one people get wrong. It looks like
/// configuration - it is set once and never changes - but it is half of a
/// credential: possession of it plus a four-digit PIN signs a cashier in. It
/// belongs with the tokens, not with the settings.
class SessionStore {
  SessionStore({FlutterSecureStorage? secure}) : _secure = secure ?? const FlutterSecureStorage();

  static const _accessKey = 'access_token';
  static const _refreshKey = 'refresh_token';
  static const _deviceTokenKey = 'device_token';

  static const _tenantSlugKey = 'tenant_slug';
  static const _tenantNameKey = 'tenant_name';
  static const _usernameKey = 'last_username';

  final FlutterSecureStorage _secure;

  // ---- Credentials -------------------------------------------------------

  Future<String?> accessToken() => _secure.read(key: _accessKey);
  Future<String?> refreshToken() => _secure.read(key: _refreshKey);
  Future<String?> deviceToken() => _secure.read(key: _deviceTokenKey);

  Future<bool> hasAccessToken() async => (await accessToken()) != null;
  Future<bool> hasRefreshToken() async => (await refreshToken()) != null;
  Future<bool> isDeviceRegistered() async => (await deviceToken()) != null;

  Future<void> saveTokens({required String access, required String refresh}) async {
    await _secure.write(key: _accessKey, value: access);
    await _secure.write(key: _refreshKey, value: refresh);
  }

  /// Store the till's registration token.
  ///
  /// Written once, when the device is set up. The server shows the plaintext
  /// exactly once, so losing this means registering the till again - which is
  /// the correct outcome for something that could otherwise be recovered by
  /// whoever finds the tablet.
  Future<void> saveDeviceToken(String token) =>
      _secure.write(key: _deviceTokenKey, value: token);

  /// Sign out the person, keeping the till registered.
  ///
  /// The distinction that makes PIN sign-in work: the *device* stays known so
  /// the next cashier can take over with four digits, while the previous
  /// cashier's tokens are gone.
  Future<void> clear() async {
    await _secure.delete(key: _accessKey);
    await _secure.delete(key: _refreshKey);
  }

  /// Forget everything, including the registration. Used when a till is being
  /// handed to a different business.
  Future<void> clearEverything() async {
    await clear();
    await _secure.delete(key: _deviceTokenKey);
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tenantSlugKey);
    await prefs.remove(_tenantNameKey);
    await prefs.remove(_usernameKey);
  }

  // ---- Preferences -------------------------------------------------------

  /// The business this till belongs to.
  ///
  /// Entered once at setup and sent automatically from then on, which is what
  /// lets usernames be unique per business rather than across the platform.
  Future<String?> tenantSlug() async =>
      (await SharedPreferences.getInstance()).getString(_tenantSlugKey);

  Future<String?> tenantName() async =>
      (await SharedPreferences.getInstance()).getString(_tenantNameKey);

  Future<void> saveTenant({required String slug, String? name}) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tenantSlugKey, slug);
    if (name != null) await prefs.setString(_tenantNameKey, name);
  }

  /// The last cashier to sign in, so the PIN screen can pre-fill their name.
  ///
  /// A convenience, not a credential: it saves typing at a busy counter and
  /// reveals nothing that possession of the till does not already reveal.
  Future<String?> lastUsername() async =>
      (await SharedPreferences.getInstance()).getString(_usernameKey);

  Future<void> saveLastUsername(String username) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_usernameKey, username);
  }
}
