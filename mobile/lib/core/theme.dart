import 'package:flutter/material.dart';

/// Visual design for a till.
///
/// Three constraints drive everything here, and none of them are aesthetic.
///
/// **Bright light.** A duka counter is often near an open front, and a phone
/// screen at half brightness in Nairobi sun is close to unreadable. So contrast
/// is pushed well past the usual minimum: near-black text on white, saturated
/// accents, and no light grey text anywhere that carries meaning.
///
/// **Fingers, not cursors.** Every interactive target is at least 56 logical
/// pixels tall - above the 48 that Material suggests - because the person
/// tapping is often holding a bag of shopping in the other hand.
///
/// **One-handed reach.** Primary actions sit at the bottom of the screen where
/// a thumb lands, not in an app bar at the top.
class TillTheme {
  const TillTheme._();

  /// Minimum height for anything tappable. Deliberately above Material's 48.
  static const double minTapTarget = 56;

  /// Height for the primary action on a screen, which is usually the one being
  /// hit repeatedly and at speed.
  static const double primaryActionHeight = 64;

  static const Color _ink = Color(0xFF101418);
  static const Color _surface = Color(0xFFFFFFFF);
  static const Color _muted = Color(0xFF4A5259);
  static const Color _brand = Color(0xFF00695C);
  static const Color _brandDark = Color(0xFF004D40);
  static const Color _danger = Color(0xFFB3261E);
  static const Color _warning = Color(0xFF8A5000);
  static const Color _ok = Color(0xFF1B5E20);
  static const Color _line = Color(0xFFD3D8DC);

  static Color get danger => _danger;
  static Color get warning => _warning;
  static Color get ok => _ok;
  static Color get muted => _muted;
  static Color get line => _line;

  static ThemeData build() {
    final base = ThemeData(useMaterial3: true, brightness: Brightness.light);

    return base.copyWith(
      colorScheme: const ColorScheme.light(
        primary: _brand,
        onPrimary: Colors.white,
        secondary: _brandDark,
        surface: _surface,
        onSurface: _ink,
        error: _danger,
        onError: Colors.white,
      ),
      scaffoldBackgroundColor: const Color(0xFFF7F9FA),

      // Text is a step larger than Material's defaults throughout. A cashier
      // reads this at arm's length across a counter, not at a desk.
      textTheme: base.textTheme
          .apply(bodyColor: _ink, displayColor: _ink)
          .copyWith(
            headlineSmall: const TextStyle(
              fontSize: 26,
              fontWeight: FontWeight.w700,
              color: _ink,
            ),
            titleLarge: const TextStyle(
              fontSize: 22,
              fontWeight: FontWeight.w700,
              color: _ink,
            ),
            titleMedium: const TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.w600,
              color: _ink,
            ),
            bodyLarge: const TextStyle(fontSize: 17, color: _ink),
            bodyMedium: const TextStyle(fontSize: 15, color: _ink),
            labelLarge: const TextStyle(
              fontSize: 17,
              fontWeight: FontWeight.w600,
            ),
          ),

      appBarTheme: const AppBarTheme(
        backgroundColor: _brand,
        foregroundColor: Colors.white,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontSize: 20,
          fontWeight: FontWeight.w700,
          color: Colors.white,
        ),
      ),

      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size.fromHeight(primaryActionHeight),
          textStyle: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          minimumSize: const Size.fromHeight(minTapTarget),
          foregroundColor: _brandDark,
          side: const BorderSide(color: _brand, width: 2),
          textStyle: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600),
        ),
      ),

      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: _surface,
        // Generous padding so the tap area is comfortably past the minimum even
        // before the label is accounted for.
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 20),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: _line, width: 1.5),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: _line, width: 1.5),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: _brand, width: 2.5),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: _danger, width: 2),
        ),
        labelStyle: const TextStyle(fontSize: 17, color: _muted),
        errorStyle: const TextStyle(fontSize: 15, color: _danger),
      ),

      cardTheme: CardThemeData(
        color: _surface,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: const BorderSide(color: _line),
        ),
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      ),

      listTileTheme: const ListTileThemeData(
        minVerticalPadding: 14,
        titleTextStyle: TextStyle(
          fontSize: 18,
          fontWeight: FontWeight.w600,
          color: _ink,
        ),
        subtitleTextStyle: TextStyle(fontSize: 15, color: _muted),
      ),

      snackBarTheme: const SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        contentTextStyle: TextStyle(fontSize: 16, color: Colors.white),
      ),
    );
  }
}
