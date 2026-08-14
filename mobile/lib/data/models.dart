/// Plain data classes mirroring the API's shapes.
///
/// Hand-written rather than generated. There are few enough of them that a code
/// generator would add a build step for very little, and the parsing is where
/// the API's contract becomes visible to a reader - which is worth keeping in
/// sight rather than hiding behind an annotation.
library;

/// Money as the API sends it: an integer number of cents, never a double.
///
/// The type exists to make that hard to get wrong on this side too. A double
/// cannot hold 0.10 exactly, so formatting through one would eventually print a
/// receipt that does not add up.
class Money {
  const Money(this.cents);

  final int cents;

  /// Formatted the way it appears on a receipt: `KES 1,234.50`.
  String format({String currency = 'KES'}) {
    final negative = cents < 0;
    final absolute = cents.abs();
    final whole = absolute ~/ 100;
    final fraction = (absolute % 100).toString().padLeft(2, '0');

    final grouped = whole.toString().replaceAllMapped(
      RegExp(r'(\d{1,3})(?=(\d{3})+$)'),
      (match) => '${match[1]},',
    );

    // No currency means no separator either. A leading space is invisible in
    // code and obvious on a receipt, where it pushes a column of figures one
    // character out of alignment.
    final prefix = currency.isEmpty ? '' : '$currency ';
    return '${negative ? '-' : ''}$prefix$grouped.$fraction';
  }

  @override
  String toString() => format();
}

/// Who is signed in, and which business they belong to.
class Session {
  const Session({
    required this.userId,
    required this.username,
    required this.fullName,
    required this.role,
    required this.tenantSlug,
    required this.tenantName,
  });

  factory Session.fromJson(Map<String, dynamic> json) {
    final user = (json['user'] ?? json) as Map<String, dynamic>;
    final tenant = json['tenant'] as Map<String, dynamic>?;

    return Session(
      userId: user['id'] as String? ?? '',
      username: user['username'] as String? ?? '',
      fullName: user['full_name'] as String? ?? '',
      role: user['role'] as String? ?? 'CASHIER',
      tenantSlug: tenant?['slug'] as String? ?? '',
      tenantName: tenant?['name'] as String? ?? '',
    );
  }

  final String userId;
  final String username;
  final String fullName;
  final String role;
  final String tenantSlug;
  final String tenantName;

  bool get isCashier => role == 'CASHIER';
  bool get canManage => role == 'MANAGER' || role == 'OWNER';

  String get roleLabel => switch (role) {
    'OWNER' => 'Owner',
    'MANAGER' => 'Manager',
    _ => 'Cashier',
  };

  /// First name only, for greeting someone at the top of a busy screen.
  String get shortName => fullName.split(' ').first;
}

class Category {
  const Category({required this.id, required this.name, required this.itemCount});

  factory Category.fromJson(Map<String, dynamic> json) => Category(
    id: json['id'] as String,
    name: json['name'] as String,
    itemCount: json['item_count'] as int? ?? 0,
  );

  final String id;
  final String name;
  final int itemCount;
}

/// How much of an item is at one branch.
class StockLevel {
  const StockLevel({
    required this.storeCode,
    required this.quantity,
    required this.isLow,
  });

  factory StockLevel.fromJson(Map<String, dynamic> json) => StockLevel(
    storeCode: json['store_code'] as String? ?? '',
    // Kept as the string the API sent. Parsing to a double to display it would
    // reintroduce exactly the imprecision the server took care to avoid.
    quantity: json['quantity'] as String? ?? '0',
    isLow: json['is_low'] as bool? ?? false,
  );

  final String storeCode;
  final String quantity;
  final bool isLow;

  /// Trailing zeros trimmed, so 40.000 reads as 40 and 2.500 as 2.5.
  String get display {
    if (!quantity.contains('.')) return quantity;
    final trimmed = quantity.replaceAll(RegExp(r'0+$'), '');
    return trimmed.endsWith('.') ? trimmed.substring(0, trimmed.length - 1) : trimmed;
  }
}

class Item {
  const Item({
    required this.id,
    required this.sku,
    required this.name,
    required this.tillLabel,
    required this.price,
    required this.isService,
    required this.isPriceVariable,
    required this.isAvailable,
    required this.tracksStock,
    required this.unit,
    required this.categoryName,
    required this.barcodes,
    required this.stock,
    required this.durationMinutes,
  });

  factory Item.fromJson(Map<String, dynamic> json) => Item(
    id: json['id'] as String,
    sku: json['sku'] as String? ?? '',
    name: json['name'] as String? ?? '',
    tillLabel: json['till_label'] as String? ?? json['name'] as String? ?? '',
    price: Money(json['price_cents'] as int? ?? 0),
    isService: json['item_type'] == 'SERVICE',
    isPriceVariable: json['is_price_variable'] as bool? ?? false,
    isAvailable: json['is_available'] as bool? ?? true,
    tracksStock: json['track_stock'] as bool? ?? false,
    unit: json['unit'] as String? ?? 'EACH',
    categoryName: json['category_name'] as String?,
    barcodes: ((json['barcodes'] as List?) ?? [])
        .map((b) => (b as Map)['code'] as String)
        .toList(),
    stock: ((json['stock'] as List?) ?? [])
        .map((s) => StockLevel.fromJson((s as Map).cast<String, dynamic>()))
        .toList(),
    durationMinutes: json['duration_minutes'] as int?,
  );

  final String id;
  final String sku;
  final String name;
  final String tillLabel;
  final Money price;
  final bool isService;
  final bool isPriceVariable;
  final bool isAvailable;
  final bool tracksStock;
  final String unit;
  final String? categoryName;
  final List<String> barcodes;
  final List<StockLevel> stock;
  final int? durationMinutes;

  /// What to show where a quantity would go.
  ///
  /// An untracked item returns null rather than zero, because "not counted" and
  /// "none left" are different things and showing 0 for a haircut would be a
  /// lie that makes a cashier hesitate.
  String? get stockSummary {
    if (!tracksStock || stock.isEmpty) return null;
    if (stock.length == 1) return stock.first.display;
    return stock.map((s) => '${s.storeCode} ${s.display}').join(' · ');
  }

  bool get isLowOnStock => stock.any((level) => level.isLow);

  /// How the price reads on a list row.
  ///
  /// A variable price shows what it is: a starting point the cashier will
  /// change, not a figure to be trusted as final.
  String get priceDisplay =>
      isPriceVariable ? 'from ${price.format()}' : price.format();
}
