from django.apps import AppConfig


class StoresConfig(AppConfig):
    """Branches. One per business today, more without a migration later."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.stores"
    verbose_name = "Stores"
