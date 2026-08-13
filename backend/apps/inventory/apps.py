from django.apps import AppConfig


class InventoryConfig(AppConfig):
    """How much of each stock-tracked item is at each branch, and why."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    verbose_name = "Inventory"
