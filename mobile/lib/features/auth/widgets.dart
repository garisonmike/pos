import 'package:flutter/material.dart';

import '../../core/theme.dart';

/// A refusal, shown where the person is already looking.
///
/// Inline rather than a snackbar: a sign-in error needs to stay on screen while
/// they retype, and a message that vanishes after four seconds is one they will
/// miss while looking at the keyboard.
class ErrorBanner extends StatelessWidget {
  const ErrorBanner({super.key, required this.message, this.icon});

  final String message;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFFFCEEEE),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: TillTheme.danger, width: 1.5),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon ?? Icons.error_outline, color: TillTheme.danger, size: 26),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              message,
              style: TextStyle(
                fontSize: 16,
                color: TillTheme.danger,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// A single key on the PIN pad.
///
/// Sized well above the minimum tap target, because this is the control used
/// most often in the whole app and it is used at speed, one-handed, sometimes
/// without looking.
class PinKey extends StatelessWidget {
  const PinKey({super.key, required this.label, this.icon, this.onTap});

  final String label;
  final IconData? icon;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: icon != null ? label : 'Digit $label',
      child: Material(
        color: icon != null ? Colors.transparent : Colors.white,
        borderRadius: BorderRadius.circular(16),
        child: InkWell(
          borderRadius: BorderRadius.circular(16),
          onTap: onTap,
          child: Container(
            height: 72,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(16),
              border: icon != null
                  ? null
                  : Border.all(color: TillTheme.line, width: 1.5),
            ),
            child: icon != null
                ? Icon(icon, size: 30)
                : Text(
                    label,
                    style: const TextStyle(
                      fontSize: 30,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
          ),
        ),
      ),
    );
  }
}

/// The filled and empty dots showing how many digits have been entered.
class PinDots extends StatelessWidget {
  const PinDots({super.key, required this.length, this.max = 4});

  final int length;
  final int max;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(max, (index) {
        final filled = index < length;
        return Container(
          margin: const EdgeInsets.symmetric(horizontal: 10),
          height: 20,
          width: 20,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: filled ? Theme.of(context).colorScheme.primary : Colors.transparent,
            border: Border.all(
              color: filled
                  ? Theme.of(context).colorScheme.primary
                  : TillTheme.muted,
              width: 2,
            ),
          ),
        );
      }),
    );
  }
}
