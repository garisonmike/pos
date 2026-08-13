from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Shared building blocks: tenancy, money, auditing, error handling."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"
