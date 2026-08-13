from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Shared building blocks: tenancy, money, auditing, error handling."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"

    def ready(self) -> None:
        """Register the startup configuration checks.

        Importing the module is what registers them; nothing else here uses it,
        which is why the import is inside ready() rather than at module level.
        """
        from apps.core import checks  # noqa: F401
