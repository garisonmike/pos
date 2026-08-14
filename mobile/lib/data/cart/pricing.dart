/// What a cart costs, computed on the till.
///
/// **This is a deliberate second implementation of `apps/sales/pricing.py`, and
/// it has to agree with it exactly.** Normally two implementations of the same
/// arithmetic is a mistake. Here the till has to price a cart with no
/// connection - a cashier cannot be told "no total until the network comes
/// back" while a customer waits - so the choice is between this and no offline
/// selling at all.
///
/// What keeps the two honest is that the server always re-prices on arrival and
/// never trusts this figure. A disagreement is recorded as a discrepancy rather
/// than silently accepted, so a drift between these two files surfaces as data
/// a person looks at instead of money quietly going missing.
///
/// Every rule below is the server's rule, for the server's reason:
///
/// **Integer cents throughout, never a double.** A double cannot hold 0.10
/// exactly. Dart's `int` is 64-bit on the VM but compiles to a JS double on the
/// web; this app is Android-only, so the arithmetic is exact.
///
/// **Discount before tax.** A discount reduces the taxable amount. Taxing first
/// would charge tax on money nobody paid.
///
/// **A cart discount is apportioned across lines before tax.** Tax is per line,
/// because each line carries its own rate and inclusive flag, so there is no
/// single rate at which a whole-cart discount could be taxed.
///
/// **Round per line, then sum.** Never round a total. Rounding the sum lets the
/// total disagree with the lines printed above it, which is exactly the thing a
/// customer checks.
library;

/// Basis points in one whole. A rate of 16% is 1600 bps.
const int kBpsDenominator = 10000;

/// The smallest coin in practical circulation, in cents.
const int kCashRoundingCents = 100;

class PricingError implements Exception {
  PricingError(this.message);
  final String message;
  @override
  String toString() => 'PricingError: $message';
}

/// Divide and round half away from zero.
///
/// Dart's `~/` truncates towards zero, and `round()` on a double reintroduces
/// the imprecision this whole file exists to avoid. Mirrors the server's
/// `round_half_up_div` including its negative branch.
int roundHalfUpDiv(int numerator, int denominator) {
  if (denominator <= 0) {
    throw PricingError('denominator must be positive');
  }
  if (numerator < 0) {
    return -((2 * -numerator + denominator) ~/ (2 * denominator));
  }
  return (2 * numerator + denominator) ~/ (2 * denominator);
}

int applyPercentage(int amountCents, int percentBps) =>
    roundHalfUpDiv(amountCents * percentBps, kBpsDenominator);

/// Tax already contained in a gross amount.
///
/// Derived by subtraction rather than computed independently, so that
/// `net + tax` is always exactly what the customer pays. Computing both and
/// hoping they add up is how a receipt ends up a cent out.
int taxFromInclusive(int grossCents, int rateBps) =>
    roundHalfUpDiv(grossCents * rateBps, kBpsDenominator + rateBps);

int taxOnExclusive(int netCents, int rateBps) =>
    roundHalfUpDiv(netCents * rateBps, kBpsDenominator);

/// Round to the smallest coin that actually circulates.
int roundCash(int amountCents, {int increment = kCashRoundingCents}) {
  if (increment <= 0) throw PricingError('increment must be positive');
  return roundHalfUpDiv(amountCents, increment) * increment;
}

/// One line as the cashier has entered it.
class LineInput {
  const LineInput({
    required this.itemId,
    required this.name,
    required this.unitPriceCents,
    required this.quantityMilli,
    this.sku = '',
    this.unit = 'EACH',
    this.taxRateBps = 0,
    this.taxIsInclusive = true,
    this.discountBps = 0,
    this.discountCents = 0,
  });

  final String itemId;
  final String name;
  final String sku;
  final String unit;
  final int unitPriceCents;

  /// Quantity in thousandths, because goods are sold by weight and 0.333 kg has
  /// to be representable exactly. An integer here for the same reason money is
  /// an integer: a double would make 2.5 kg of sugar occasionally price a cent
  /// differently than the server did.
  final int quantityMilli;

  final int taxRateBps;
  final bool taxIsInclusive;
  final int discountBps;
  final int discountCents;

  double get quantity => quantityMilli / 1000;

  /// Quantity times price, rounded once.
  ///
  /// Rounded here because quantity is fractional for goods sold by weight -
  /// 2.5 kg at KES 180 is exact, but 0.333 kg is not, and the cent has to be
  /// resolved somewhere. Doing it once, at the line, keeps every later figure
  /// exact.
  int grossBeforeDiscount() =>
      roundHalfUpDiv(unitPriceCents * quantityMilli, 1000);

  LineInput copyWith({int? quantityMilli, int? unitPriceCents, int? discountBps, int? discountCents}) =>
      LineInput(
        itemId: itemId,
        name: name,
        sku: sku,
        unit: unit,
        unitPriceCents: unitPriceCents ?? this.unitPriceCents,
        quantityMilli: quantityMilli ?? this.quantityMilli,
        taxRateBps: taxRateBps,
        taxIsInclusive: taxIsInclusive,
        discountBps: discountBps ?? this.discountBps,
        discountCents: discountCents ?? this.discountCents,
      );
}

/// One priced line.
class LineTotals {
  const LineTotals({
    required this.line,
    required this.grossBeforeDiscountCents,
    required this.lineDiscountCents,
    required this.cartDiscountShareCents,
    required this.netCents,
    required this.taxCents,
    required this.grossCents,
  });

  final LineInput line;
  final int grossBeforeDiscountCents;
  final int lineDiscountCents;
  final int cartDiscountShareCents;
  final int netCents;
  final int taxCents;
  final int grossCents;

  int get totalDiscountCents => lineDiscountCents + cartDiscountShareCents;
}

/// A whole priced cart.
class CartTotals {
  const CartTotals({
    required this.lines,
    required this.subtotalCents,
    required this.discountCents,
    required this.taxCents,
    required this.totalCents,
  });

  static const empty = CartTotals(
    lines: [],
    subtotalCents: 0,
    discountCents: 0,
    taxCents: 0,
    totalCents: 0,
  );

  final List<LineTotals> lines;
  final int subtotalCents;
  final int discountCents;
  final int taxCents;
  final int totalCents;

  int get lineCount => lines.length;

  /// What the customer is asked for in cash, rounded to the shilling.
  int get cashDueCents => roundCash(totalCents);

  /// The difference rounding made, recorded so the drawer reconciles exactly
  /// rather than drifting a few shillings a day.
  int get roundingAdjustmentCents => cashDueCents - totalCents;
}

/// Work out a discount in cents, however it was expressed.
///
/// Percentage and fixed amount may both be present, and both apply. Capped at
/// the base, because a discount larger than the thing being discounted would
/// produce a negative line and, downstream, negative tax.
int resolveDiscount(int baseCents, {int discountBps = 0, int discountCents = 0}) {
  if (discountBps < 0 || discountCents < 0) {
    throw PricingError('A discount cannot be negative.');
  }
  if (discountBps > kBpsDenominator) {
    throw PricingError('A discount cannot exceed 100%.');
  }
  final total = applyPercentage(baseCents, discountBps) + discountCents;
  final cap = baseCents > 0 ? baseCents : 0;
  return total < cap ? total : cap;
}

/// Split an amount across weights so the parts sum exactly to the whole.
///
/// Largest-remainder, ties broken by the larger weight then by index, so the
/// result is deterministic rather than dependent on iteration order. The
/// exactness is the point: a cart discount that apportions to one cent less
/// than it should leaves a total that does not match the sum of its lines, and
/// the difference gets silently absorbed into tax.
List<int> apportion(int amountCents, List<int> weights) {
  if (amountCents == 0 || weights.isEmpty) {
    return List<int>.filled(weights.length, 0);
  }
  if (weights.any((w) => w < 0)) {
    throw PricingError('Weights cannot be negative.');
  }

  final totalWeight = weights.fold<int>(0, (a, b) => a + b);
  if (totalWeight == 0) {
    // Every line is free, so there is nothing to take a discount off. Splitting
    // equally instead would create negative lines.
    return List<int>.filled(weights.length, 0);
  }

  final shares = [
    for (final weight in weights) (amountCents * weight) ~/ totalWeight,
  ];
  var remainder = amountCents - shares.fold<int>(0, (a, b) => a + b);

  if (remainder != 0) {
    final fractions = [
      for (final weight in weights) (amountCents * weight) % totalWeight,
    ];
    final order = List<int>.generate(weights.length, (i) => i)
      ..sort((a, b) {
        final byFraction = fractions[b].compareTo(fractions[a]);
        if (byFraction != 0) return byFraction;
        final byWeight = weights[b].compareTo(weights[a]);
        if (byWeight != 0) return byWeight;
        return a.compareTo(b);
      });
    for (var position = 0; position < remainder; position++) {
      shares[order[position % order.length]] += 1;
    }
  }

  return shares;
}

/// Price a whole cart, lines and all.
///
/// Line gross, then line discount, then apportion the cart discount over what
/// remains, then split tax on each line using that line's own rate and
/// inclusive flag. Mixed inclusive and exclusive lines therefore total
/// correctly on one sale, which a per-business tax setting could not express.
CartTotals priceCart(
  List<LineInput> lines, {
  int cartDiscountBps = 0,
  int cartDiscountCents = 0,
}) {
  if (lines.isEmpty) throw PricingError('A sale needs at least one line.');

  final grosses = [for (final line in lines) line.grossBeforeDiscount()];
  final lineDiscounts = [
    for (var i = 0; i < lines.length; i++)
      resolveDiscount(
        grosses[i],
        discountBps: lines[i].discountBps,
        discountCents: lines[i].discountCents,
      ),
  ];
  final afterLineDiscount = [
    for (var i = 0; i < lines.length; i++) grosses[i] - lineDiscounts[i],
  ];

  final cartDiscount = resolveDiscount(
    afterLineDiscount.fold<int>(0, (a, b) => a + b),
    discountBps: cartDiscountBps,
    discountCents: cartDiscountCents,
  );
  final shares = apportion(cartDiscount, afterLineDiscount);

  final priced = <LineTotals>[];
  var subtotal = 0, discount = 0, tax = 0, total = 0;

  for (var i = 0; i < lines.length; i++) {
    final line = lines[i];
    final charged = afterLineDiscount[i] - shares[i];

    int net, lineTax, lineGross;
    if (line.taxRateBps <= 0) {
      net = charged;
      lineTax = 0;
      lineGross = charged;
    } else if (line.taxIsInclusive) {
      lineTax = taxFromInclusive(charged, line.taxRateBps);
      net = charged - lineTax;
      lineGross = charged;
    } else {
      lineTax = taxOnExclusive(charged, line.taxRateBps);
      net = charged;
      lineGross = charged + lineTax;
    }

    priced.add(
      LineTotals(
        line: line,
        grossBeforeDiscountCents: grosses[i],
        lineDiscountCents: lineDiscounts[i],
        cartDiscountShareCents: shares[i],
        netCents: net,
        taxCents: lineTax,
        grossCents: lineGross,
      ),
    );

    subtotal += net;
    discount += lineDiscounts[i] + shares[i];
    tax += lineTax;
    total += lineGross;
  }

  return CartTotals(
    lines: priced,
    subtotalCents: subtotal,
    discountCents: discount,
    taxCents: tax,
    totalCents: total,
  );
}

/// Change owed on a cash tender.
int changeFor(int tenderedCents, int dueCents) {
  if (tenderedCents < dueCents) {
    throw PricingError('Tendered less than the amount due.');
  }
  return tenderedCents - dueCents;
}
