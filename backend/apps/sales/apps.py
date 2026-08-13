from django.apps import AppConfig


class SalesConfig(AppConfig):
    """Sales, their payments and their refunds."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"
    verbose_name = "Sales"
