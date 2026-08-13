from django.apps import AppConfig


class TenantsConfig(AppConfig):
    """The businesses using the platform, and what each has switched on."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"
    verbose_name = "Tenants"
