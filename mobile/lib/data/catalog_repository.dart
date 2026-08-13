import '../core/api_client.dart';
import 'models.dart';

/// Reading the catalogue.
///
/// Read-only for this milestone. The till browses and looks things up; editing
/// prices and stock is done by a manager on the web side, and selling arrives
/// with milestone 3.
class CatalogRepository {
  CatalogRepository(this._api);

  final ApiClient _api;

  Future<List<Category>> categories() async {
    final result = await _api.get('/api/v1/categories/', query: {'page_size': 100});
    return result.asList
        .map((row) => Category.fromJson((row as Map).cast<String, dynamic>()))
        .toList();
  }

  /// A page of items, optionally filtered to one category.
  Future<List<Item>> items({String? categoryId, int page = 1}) async {
    final result = await _api.get(
      '/api/v1/items/',
      query: {
        'page': page,
        'page_size': 50,
        'is_active': true,
        if (categoryId != null) 'category': categoryId,
      },
    );
    return result.asList
        .map((row) => Item.fromJson((row as Map).cast<String, dynamic>()))
        .toList();
  }

  /// Type-ahead search across name, short name, SKU and barcode.
  ///
  /// Returns the trimmed shape the search endpoint sends, which carries enough
  /// for a list row and nothing more - the full record is fetched only if
  /// someone opens the item.
  Future<List<Item>> search(String query) async {
    if (query.trim().length < 2) return [];

    final result = await _api.get('/api/v1/items/search/', query: {'q': query.trim()});
    return (result.data as List)
        .map((row) => Item.fromJson((row as Map).cast<String, dynamic>()))
        .toList();
  }

  /// Resolve a scanned barcode to its item.
  ///
  /// Returns null when nothing matches, rather than throwing. An unrecognised
  /// barcode is an ordinary event at a counter - a new line the shop has not
  /// added yet - not an error worth an exception.
  Future<Item?> lookupBarcode(String code) async {
    try {
      final result = await _api.get('/api/v1/items/lookup/', query: {'barcode': code});
      return Item.fromJson(result.asMap);
    } on ApiException catch (error) {
      if (error.code == 'barcode_not_found') return null;
      rethrow;
    }
  }

  Future<Item> item(String id) async {
    final result = await _api.get('/api/v1/items/$id/');
    return Item.fromJson(result.asMap);
  }
}
