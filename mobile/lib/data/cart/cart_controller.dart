/// The cart a cashier is building, and what it comes to.
///
/// Held apart from any widget so it can be tested without a widget tree, and
/// because the same cart has to survive the cashier moving between the item
/// list, the search field and the checkout sheet.
///
/// Totals are never stored. They are derived from the lines every time, by
/// [priceCart], so there is no cached figure that can fall out of step with
/// what is actually in the cart - which is the way a total ends up disagreeing
/// with the lines printed above it.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'pricing.dart';

/// A cart, as the till holds it.
class CartState {
  const CartState({
    this.lines = const [],
    this.cartDiscountBps = 0,
    this.cartDiscountCents = 0,
    this.discountReason = '',
    this.approvedBy,
  });

  final List<LineInput> lines;
  final int cartDiscountBps;
  final int cartDiscountCents;

  /// Required whenever a discount is applied. Not optional at this layer
  /// either: the server refuses a discount with no reason, and letting the till
  /// build one anyway would only produce a checkout that fails at the end.
  final String discountReason;

  /// Who authorised the discount, once somebody has. Null until then.
  final CartApproval? approvedBy;

  bool get isEmpty => lines.isEmpty;
  bool get isNotEmpty => lines.isNotEmpty;

  bool get hasDiscount =>
      cartDiscountBps > 0 ||
      cartDiscountCents > 0 ||
      lines.any((line) => line.discountBps > 0 || line.discountCents > 0);

  /// Whether this cart may be taken to checkout.
  ///
  /// A discount with nobody's name against it is the one thing that blocks it.
  /// Everything else the server can decide.
  bool get needsAuthorization => hasDiscount && approvedBy == null;

  CartTotals get totals => lines.isEmpty
      ? CartTotals.empty
      : priceCart(
          lines,
          cartDiscountBps: cartDiscountBps,
          cartDiscountCents: cartDiscountCents,
        );

  /// Total units in the cart, for the badge on the cart button.
  int get itemCount => lines.length;

  CartState copyWith({
    List<LineInput>? lines,
    int? cartDiscountBps,
    int? cartDiscountCents,
    String? discountReason,
    CartApproval? approvedBy,
    bool clearApproval = false,
  }) =>
      CartState(
        lines: lines ?? this.lines,
        cartDiscountBps: cartDiscountBps ?? this.cartDiscountBps,
        cartDiscountCents: cartDiscountCents ?? this.cartDiscountCents,
        discountReason: discountReason ?? this.discountReason,
        approvedBy: clearApproval ? null : (approvedBy ?? this.approvedBy),
      );
}

/// Who approved a discount, and how.
class CartApproval {
  const CartApproval({
    required this.username,
    required this.reason,
    required this.method,
    required this.at,
    this.pinVersion,
  });

  final String username;
  final String reason;

  /// `SESSION` when a manager rang it up themselves, `PIN` when one approved at
  /// the till, `OFFLINE` when the device checked its cached copy.
  final String method;

  final DateTime at;

  /// Only set for an offline approval: which version of the cached PIN was
  /// checked. Sent to the server so an approval made against a PIN that has
  /// since changed is detectable.
  final int? pinVersion;

  bool get isOffline => method == 'OFFLINE';
}

class CartController extends StateNotifier<CartState> {
  CartController() : super(const CartState());

  /// Add an item, or add to the line that is already there.
  ///
  /// Merging rather than appending a second line, because a cashier scanning
  /// the same barcode twice means two of that thing - and a receipt listing it
  /// on two lines makes a customer think they have been charged twice.
  ///
  /// A variable-priced item is the exception: two haircuts at different prices
  /// are genuinely two lines, and merging them would silently discard one of
  /// the prices the cashier typed.
  void add(
    LineInput line, {
    bool mergeable = true,
  }) {
    if (!mergeable) {
      state = state.copyWith(lines: [...state.lines, line]);
      return;
    }

    final index = state.lines.indexWhere(
      (existing) =>
          existing.itemId == line.itemId &&
          existing.unitPriceCents == line.unitPriceCents &&
          existing.discountBps == line.discountBps &&
          existing.discountCents == line.discountCents,
    );

    if (index < 0) {
      state = state.copyWith(lines: [...state.lines, line]);
      return;
    }

    final updated = [...state.lines];
    updated[index] = updated[index].copyWith(
      quantityMilli: updated[index].quantityMilli + line.quantityMilli,
    );
    state = state.copyWith(lines: updated);
  }

  void setQuantity(int index, int quantityMilli) {
    if (index < 0 || index >= state.lines.length) return;
    if (quantityMilli <= 0) {
      removeAt(index);
      return;
    }
    final updated = [...state.lines];
    updated[index] = updated[index].copyWith(quantityMilli: quantityMilli);
    state = state.copyWith(lines: updated);
  }

  void removeAt(int index) {
    if (index < 0 || index >= state.lines.length) return;
    final updated = [...state.lines]..removeAt(index);
    state = state.copyWith(lines: updated);
  }

  /// Discount one line.
  ///
  /// Any change to a discount clears an approval that was already given. The
  /// manager approved the discount they were shown, not whatever it was
  /// afterwards edited into.
  void discountLine(int index, {int bps = 0, int cents = 0}) {
    if (index < 0 || index >= state.lines.length) return;
    final updated = [...state.lines];
    updated[index] = updated[index].copyWith(discountBps: bps, discountCents: cents);
    state = state.copyWith(lines: updated, clearApproval: true);
  }

  void discountCart({int bps = 0, int cents = 0, String reason = ''}) {
    state = state.copyWith(
      cartDiscountBps: bps,
      cartDiscountCents: cents,
      discountReason: reason,
      clearApproval: true,
    );
  }

  void approve(CartApproval approval) {
    state = state.copyWith(approvedBy: approval, discountReason: approval.reason);
  }

  /// Empty the cart, after a sale is finished or abandoned.
  void clear() => state = const CartState();
}

final cartControllerProvider =
    StateNotifierProvider<CartController, CartState>((ref) => CartController());

/// What the cart currently comes to.
///
/// A separate provider so a widget showing only the total does not rebuild for
/// every keystroke in a quantity field that did not change the figure.
final cartTotalsProvider = Provider<CartTotals>(
  (ref) => ref.watch(cartControllerProvider).totals,
);
