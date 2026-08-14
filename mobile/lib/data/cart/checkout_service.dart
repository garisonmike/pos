/// Taking the money, whether or not there is a network.
///
/// One decision matters more than anything else here: **the `client_uuid` is
/// generated before the online attempt, and the same one is reused if the sale
/// has to be queued.**
///
/// The normal failure on Kenyan mobile data is not a connection that drops. It
/// is a request that hangs for ninety seconds and *succeeded invisibly*. If the
/// till queued that sale under a fresh identifier, the sync would create a
/// second one and the customer would be charged twice in the shop's books.
/// Reusing the identifier means the server recognises the replay and answers
/// `duplicate`, which costs nothing.
///
/// The second rule: **a sale is never lost to a failed request.** The cash is in
/// the drawer by the time this runs. Every path that is not a definite refusal
/// ends with the sale in the outbox.
library;

import 'package:uuid/uuid.dart';

import '../../core/api_client.dart';
import '../outbox/outbox_repository.dart';
import 'cart_controller.dart';
import 'pricing.dart';

/// How a completed sale ended up.
enum CheckoutOutcome {
  /// The server took it and gave it a receipt number.
  settled,

  /// It is in the outbox. The customer gets a provisional reference.
  queued,

  /// The server refused it, and retrying unchanged will not help.
  refused,
}

class CheckoutResult {
  const CheckoutResult({
    required this.outcome,
    required this.clientUuid,
    required this.totals,
    required this.tenderedCents,
    required this.changeCents,
    this.receiptNumber,
    this.provisionalReference,
    this.saleId,
    this.error,
  });

  final CheckoutOutcome outcome;
  final String clientUuid;
  final CartTotals totals;
  final int tenderedCents;
  final int changeCents;

  final int? receiptNumber;
  final String? provisionalReference;
  final String? saleId;
  final ApiException? error;

  bool get isQueued => outcome == CheckoutOutcome.queued;
  bool get isRefused => outcome == CheckoutOutcome.refused;

  /// What to print where a receipt number goes.
  String get reference =>
      receiptNumber != null ? '#$receiptNumber' : (provisionalReference ?? '');
}

/// The one thing checkout needs from the network.
///
/// Narrower than [ApiClient], for the same reason the outbox has its own port:
/// deciding whether a failure means "queue it" or "refuse it" is the part worth
/// testing, and it should not require a Dio and a platform keystore to ask.
abstract class CheckoutTransport {
  Future<Map<String, dynamic>> postCashSale(Map<String, dynamic> body);
}

/// [CheckoutTransport] over the real API.
class ApiCheckoutTransport implements CheckoutTransport {
  const ApiCheckoutTransport(this._api);

  final ApiClient _api;

  @override
  Future<Map<String, dynamic>> postCashSale(Map<String, dynamic> body) async {
    final result = await _api.post('/api/v1/sales/checkout/cash/', body: body);
    return result.asMap;
  }
}

class CheckoutService {
  CheckoutService({
    required CheckoutTransport transport,
    required OutboxRepository outbox,
    Uuid? uuid,
  })  : _transport = transport,
        _outbox = outbox,
        _uuid = uuid ?? const Uuid();

  final CheckoutTransport _transport;
  final OutboxRepository _outbox;
  final Uuid _uuid;

  /// Ring up a cash sale.
  ///
  /// [forceOffline] exists for the case where the till already knows it has no
  /// connection - there is no point making a cashier wait out a timeout the app
  /// can already predict, with a customer standing at the counter.
  Future<CheckoutResult> takeCash({
    required CartState cart,
    required int tenderedCents,
    String? storeId,
    String customerPhone = '',
    String note = '',
    bool forceOffline = false,
  }) async {
    final totals = cart.totals;
    final dueCents = totals.cashDueCents;
    final changeCents = changeFor(tenderedCents, dueCents);

    // Generated here, before anything is attempted, so that the online request
    // and any later queued replay carry the *same* key.
    final clientUuid = _uuid.v4();

    if (!forceOffline) {
      try {
        final sale = await _transport.postCashSale(
          _onlineBody(
            cart: cart,
            totals: totals,
            tenderedCents: tenderedCents,
            clientUuid: clientUuid,
            storeId: storeId,
            customerPhone: customerPhone,
            note: note,
          ),
        );
        return CheckoutResult(
          outcome: CheckoutOutcome.settled,
          clientUuid: clientUuid,
          totals: totals,
          tenderedCents: tenderedCents,
          changeCents: changeCents,
          receiptNumber: sale['receipt_number'] as int?,
          saleId: sale['id'] as String?,
        );
      } on ApiException catch (error) {
        if (!_shouldQueue(error)) {
          // A definite refusal - an unknown item, a discount with no authority.
          // Queueing it would only reproduce the same refusal at sync, with the
          // cashier no longer at the counter to fix it.
          return CheckoutResult(
            outcome: CheckoutOutcome.refused,
            clientUuid: clientUuid,
            totals: totals,
            tenderedCents: tenderedCents,
            changeCents: changeCents,
            error: error,
          );
        }
      }
    }

    final queued = await _outbox.enqueue(
      totals: totals,
      tenderedCents: tenderedCents,
      storeId: storeId,
      customerPhone: customerPhone,
      note: note,
      approval: _approvalFor(cart),
      clientUuid: clientUuid,
    );

    return CheckoutResult(
      outcome: CheckoutOutcome.queued,
      clientUuid: clientUuid,
      totals: totals,
      tenderedCents: tenderedCents,
      changeCents: changeCents,
      provisionalReference: provisionalReferenceFor(queued.deviceSequence),
    );
  }

  /// Whether a failure means "try again later" rather than "this is wrong".
  ///
  /// Anything that looks like connectivity is queued. So is a server error: a
  /// 500 says nothing about whether the sale was written, and the till has cash
  /// in the drawer either way. The `client_uuid` makes finding out safe.
  bool _shouldQueue(ApiException error) =>
      error.isOffline || error.status == 0 || error.status >= 500;

  Map<String, dynamic> _onlineBody({
    required CartState cart,
    required CartTotals totals,
    required int tenderedCents,
    required String clientUuid,
    String? storeId,
    String customerPhone = '',
    String note = '',
  }) {
    final approval = cart.approvedBy;
    return {
      'client_uuid': clientUuid,
      'lines': [
        for (final line in totals.lines)
          {
            'item_id': line.line.itemId,
            'quantity': (line.line.quantityMilli / 1000).toStringAsFixed(3),
            if (line.line.discountBps > 0) 'discount_bps': line.line.discountBps,
            if (line.line.discountCents > 0) 'discount_cents': line.line.discountCents,
          },
      ],
      if (cart.cartDiscountBps > 0) 'cart_discount_bps': cart.cartDiscountBps,
      if (cart.cartDiscountCents > 0) 'cart_discount_cents': cart.cartDiscountCents,
      if (approval != null)
        'discount_authorization': {
          // A manager ringing up sends only a reason; their session is the
          // authority. A cashier's request carries the manager's username, and
          // the credential was already verified when they entered it.
          if (approval.method != 'SESSION') 'username': approval.username,
          'reason': approval.reason,
        },
      'tendered_cents': tenderedCents,
      'round_to_shilling': true,
      if (storeId != null) 'store_id': storeId,
      if (customerPhone.isNotEmpty) 'customer_phone': customerPhone,
      if (note.isNotEmpty) 'note': note,
    };
  }

  OfflineApproval? _approvalFor(CartState cart) {
    final approval = cart.approvedBy;
    if (approval == null) return null;
    return OfflineApproval(
      username: approval.username,
      reason: approval.reason,
      // A session approval made offline still carries a version: the manager's
      // own cached record is what the device checked they were.
      pinVersion: approval.pinVersion ?? 0,
      authorizedAt: approval.at,
    );
  }
}

/// What a customer is given for a sale the server has not seen yet.
///
/// Derived from the till's own sequence, so it is unique on this device and
/// says which till took the money. Deliberately not shaped like a real receipt
/// number: a customer or a cashier who mistook one for the other would quote a
/// number that belongs to a different sale entirely.
String provisionalReferenceFor(int deviceSequence) =>
    'TMP-${deviceSequence.toString().padLeft(5, '0')}';
