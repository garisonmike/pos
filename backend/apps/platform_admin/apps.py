from django.apps import AppConfig


class PlatformAdminConfig(AppConfig):
    """The platform operator's own surface: onboarding, suspension, usage."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.platform_admin"
    verbose_name = "Platform administration"
