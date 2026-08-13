from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """M-Pesa: per-tenant credentials, STK pushes, and the callbacks they produce."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payments"
    verbose_name = "Payments"
