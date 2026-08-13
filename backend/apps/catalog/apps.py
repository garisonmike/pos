from django.apps import AppConfig


class CatalogConfig(AppConfig):
    """What a business sells: products, services, categories and tax rates."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog"
    verbose_name = "Catalog"
