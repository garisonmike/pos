/// Report shapes, as the API sends them.
///
/// Parsed rather than reshaped. The server decided what a figure means and how
/// it should be read - a rate beside its denominator, a variance beside its
/// explanation and not folded into it - and re-deriving any of that here would
/// give the till a second opinion about money.
///
/// Every amount stays an integer of cents on this side too.
library;

/// Money taken, by how it arrived.
class TenderSplit {
  const TenderSplit({this.cashCents = 0, this.mpesaCents = 0, this.totalCents = 0});

  factory TenderSplit.fromJson(Map<String, dynamic> json) => TenderSplit(
        cashCents: json['cash_cents'] as int? ?? 0,
        mpesaCents: json['mpesa_cents'] as int? ?? 0,
        totalCents: json['total_cents'] as int? ?? 0,
      );

  final int cashCents;
  final int mpesaCents;
  final int totalCents;
}

/// One period's takings.
class SalesSummary {
  const SalesSummary({
    required this.label,
    required this.saleCount,
    required this.grossCents,
    required this.netCents,
    required this.taxCents,
    required this.discountCents,
    required this.taken,
    required this.refunded,
    required this.refundCount,
    required this.netTakenCents,
    required this.averageBasketCents,
    required this.refundRateBps,
    required this.voidCount,
    required this.offlineSaleCount,
  });

  factory SalesSummary.fromJson(Map<String, dynamic> json) => SalesSummary(
        label: json['label'] as String? ?? '',
        saleCount: json['sale_count'] as int? ?? 0,
        grossCents: json['gross_cents'] as int? ?? 0,
        netCents: json['net_cents'] as int? ?? 0,
        taxCents: json['tax_cents'] as int? ?? 0,
        discountCents: json['discount_cents'] as int? ?? 0,
        taken: TenderSplit.fromJson(
          (json['taken'] as Map?)?.cast<String, dynamic>() ?? const {},
        ),
        refunded: TenderSplit.fromJson(
          (json['refunded'] as Map?)?.cast<String, dynamic>() ?? const {},
        ),
        refundCount: json['refund_count'] as int? ?? 0,
        netTakenCents: json['net_taken_cents'] as int? ?? 0,
        averageBasketCents: json['average_basket_cents'] as int? ?? 0,
        refundRateBps: json['refund_rate_bps'] as int? ?? 0,
        voidCount: json['void_count'] as int? ?? 0,
        offlineSaleCount: json['offline_sale_count'] as int? ?? 0,
      );

  final String label;
  final int saleCount;
  final int grossCents;
  final int netCents;
  final int taxCents;
  final int discountCents;
  final TenderSplit taken;
  final TenderSplit refunded;
  final int refundCount;
  final int netTakenCents;
  final int averageBasketCents;
  final int refundRateBps;
  final int voidCount;
  final int offlineSaleCount;

  /// A rate as a person reads it. Rendered here, at the last moment, from the
  /// basis points the server sent - never stored as a double anywhere.
  String get refundRate => '${(refundRateBps / 100).toStringAsFixed(2)}%';
}

class BestSeller {
  const BestSeller({
    required this.name,
    required this.sku,
    required this.quantity,
    required this.revenueCents,
    required this.lineCount,
  });

  factory BestSeller.fromJson(Map<String, dynamic> json) => BestSeller(
        name: json['name'] as String? ?? '',
        sku: json['sku'] as String? ?? '',
        // Kept as the string the server sent. Parsing to a double to display it
        // would reintroduce exactly the imprecision the server avoided.
        quantity: json['quantity'] as String? ?? '0',
        revenueCents: json['revenue_cents'] as int? ?? 0,
        lineCount: json['line_count'] as int? ?? 0,
      );

  final String name;
  final String sku;
  final String quantity;
  final int revenueCents;
  final int lineCount;

  /// Trailing zeros trimmed, so 40.000 reads as 40 and 2.500 as 2.5.
  String get quantityDisplay {
    if (!quantity.contains('.')) return quantity;
    final trimmed = quantity.replaceAll(RegExp(r'0+$'), '');
    return trimmed.endsWith('.')
        ? trimmed.substring(0, trimmed.length - 1)
        : trimmed;
  }
}

/// One cashier's figures.
///
/// **The denominators travel with the rates**, because the server sends them
/// that way on purpose. A discount rate on its own supports no conclusion, and
/// a screen that showed only the rate would undo a decision made deliberately
/// on the other side.
class CashierFigures {
  const CashierFigures({
    required this.username,
    required this.fullName,
    required this.saleCount,
    required this.grossCents,
    required this.discountCents,
    required this.discountedSaleCount,
    required this.voidCount,
    required this.refundCount,
    required this.averageBasketCents,
    required this.discountRateBps,
    required this.voidRateBps,
  });

  factory CashierFigures.fromJson(Map<String, dynamic> json) => CashierFigures(
        username: json['username'] as String? ?? '',
        fullName: json['full_name'] as String? ?? '',
        saleCount: json['sale_count'] as int? ?? 0,
        grossCents: json['gross_cents'] as int? ?? 0,
        discountCents: json['discount_cents'] as int? ?? 0,
        discountedSaleCount: json['discounted_sale_count'] as int? ?? 0,
        voidCount: json['void_count'] as int? ?? 0,
        refundCount: json['refund_count'] as int? ?? 0,
        averageBasketCents: json['average_basket_cents'] as int? ?? 0,
        discountRateBps: json['discount_rate_bps'] as int? ?? 0,
        voidRateBps: json['void_rate_bps'] as int? ?? 0,
      );

  final String username;
  final String fullName;
  final int saleCount;
  final int grossCents;
  final int discountCents;
  final int discountedSaleCount;
  final int voidCount;
  final int refundCount;
  final int averageBasketCents;
  final int discountRateBps;
  final int voidRateBps;

  String get displayName => fullName.isNotEmpty ? fullName : username;
  String get discountRate => '${(discountRateBps / 100).toStringAsFixed(2)}%';
}

/// The cashier report, with the note the server sends beside it.
class CashierReport {
  const CashierReport({required this.label, required this.cashiers, required this.note});

  factory CashierReport.fromJson(Map<String, dynamic> json) => CashierReport(
        label: json['label'] as String? ?? '',
        cashiers: [
          for (final row in (json['cashiers'] as List? ?? const []))
            CashierFigures.fromJson((row as Map).cast<String, dynamic>()),
        ],
        // Carried through to the screen rather than dropped. The server sends
        // it because the framing is part of the report, not decoration.
        note: json['note'] as String? ?? '',
      );

  final String label;
  final List<CashierFigures> cashiers;
  final String note;
}

/// One payment that reached a shift after it had been closed.
class LateArrival {
  const LateArrival({
    required this.saleId,
    required this.amountCents,
    required this.method,
  });

  factory LateArrival.fromJson(Map<String, dynamic> json) => LateArrival(
        saleId: json['sale_id'] as String? ?? '',
        amountCents: json['amount_cents'] as int? ?? 0,
        method: json['method'] as String? ?? '',
      );

  final String saleId;
  final int amountCents;
  final String method;
}

/// A shift as counted, and whatever arrived afterwards.
///
/// The two halves are parsed into **separate fields** and stay separate all the
/// way to the screen, mirroring the API. The server keeps them structurally
/// apart so nothing can accidentally add them; doing the addition here would
/// defeat that from the other end.
class DrawerReconciliation {
  const DrawerReconciliation({
    required this.shiftId,
    required this.cashier,
    required this.storeCode,
    required this.state,
    required this.openingFloatCents,
    required this.declaredClosingCents,
    required this.expectedClosingCents,
    required this.varianceCents,
    required this.lateCount,
    required this.lateCashCents,
    required this.late,
    required this.explainedVarianceCents,
    required this.isReconciled,
  });

  factory DrawerReconciliation.fromJson(Map<String, dynamic> json) {
    final counted = (json['counted'] as Map?)?.cast<String, dynamic>() ?? const {};
    final after =
        (json['arrived_after_close'] as Map?)?.cast<String, dynamic>() ?? const {};

    return DrawerReconciliation(
      shiftId: json['shift_id'] as String? ?? '',
      cashier: json['cashier'] as String? ?? '',
      storeCode: json['store_code'] as String? ?? '',
      state: json['state'] as String? ?? '',
      openingFloatCents: counted['opening_float_cents'] as int? ?? 0,
      declaredClosingCents: counted['declared_closing_cents'] as int?,
      expectedClosingCents: counted['expected_closing_cents'] as int?,
      varianceCents: counted['variance_cents'] as int?,
      lateCount: after['count'] as int? ?? 0,
      lateCashCents: after['cash_cents'] as int? ?? 0,
      late: [
        for (final row in (after['payments'] as List? ?? const []))
          LateArrival.fromJson((row as Map).cast<String, dynamic>()),
      ],
      explainedVarianceCents: json['explained_variance_cents'] as int?,
      isReconciled: json['is_reconciled'] as bool? ?? true,
    );
  }

  final String shiftId;
  final String cashier;
  final String storeCode;
  final String state;

  /// Signed for. Never recomputed, on either side.
  final int openingFloatCents;
  final int? declaredClosingCents;
  final int? expectedClosingCents;
  final int? varianceCents;

  /// Arrived afterwards. Shown beside the above, never merged into it.
  final int lateCount;
  final int lateCashCents;
  final List<LateArrival> late;

  /// What the variance would have read as. An explanation, not a correction.
  final int? explainedVarianceCents;

  final bool isReconciled;

  bool get isOpen => state == 'OPEN';
  bool get isShort => (varianceCents ?? 0) < 0;
  bool get isOver => (varianceCents ?? 0) > 0;
}

/// The whole shift report for a period.
class DrawerReport {
  const DrawerReport({
    required this.label,
    required this.shifts,
    required this.cashTakenCents,
    required this.unreconciledCount,
    required this.note,
  });

  factory DrawerReport.fromJson(Map<String, dynamic> json) => DrawerReport(
        label: json['label'] as String? ?? '',
        shifts: [
          for (final row in (json['shifts'] as List? ?? const []))
            DrawerReconciliation.fromJson((row as Map).cast<String, dynamic>()),
        ],
        cashTakenCents: json['cash_taken_in_period_cents'] as int? ?? 0,
        unreconciledCount: json['unreconciled_shift_count'] as int? ?? 0,
        note: json['note'] as String? ?? '',
      );

  final String label;
  final List<DrawerReconciliation> shifts;

  /// Cash recorded across the whole period, whatever drawer it landed in. The
  /// figure the drawers are compared *against*, and the reason they can differ.
  final int cashTakenCents;

  final int unreconciledCount;
  final String note;
}

/// The drawer a cashier currently has open.
class OpenShift {
  const OpenShift({
    required this.id,
    required this.cashierUsername,
    required this.openingFloatCents,
    required this.openedAt,
  });

  factory OpenShift.fromJson(Map<String, dynamic> json) => OpenShift(
        id: json['id'] as String? ?? '',
        cashierUsername: json['cashier_username'] as String? ?? '',
        openingFloatCents: json['opening_float_cents'] as int? ?? 0,
        openedAt: DateTime.tryParse(json['opened_at'] as String? ?? ''),
      );

  final String id;
  final String cashierUsername;
  final int openingFloatCents;
  final DateTime? openedAt;
}

/// A drawer that has just been closed.
///
/// The expected figure appears here and **only** here - the API does not report
/// it for an open drawer, so a cashier cannot see what to type. The blind count
/// is the server's rule; this type simply has nowhere to leak it from.
class ClosedShift {
  const ClosedShift({
    required this.declaredCents,
    required this.expectedCents,
    required this.varianceCents,
  });

  factory ClosedShift.fromJson(Map<String, dynamic> json) => ClosedShift(
        declaredCents: json['declared_closing_cents'] as int? ?? 0,
        expectedCents: json['expected_closing_cents'] as int? ?? 0,
        varianceCents: json['variance_cents'] as int? ?? 0,
      );

  final int declaredCents;
  final int expectedCents;
  final int varianceCents;

  bool get balanced => varianceCents == 0;
  bool get isShort => varianceCents < 0;
}
