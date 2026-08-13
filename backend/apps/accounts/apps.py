from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Users, roles, sign-in, and the devices a business has registered."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Accounts"
