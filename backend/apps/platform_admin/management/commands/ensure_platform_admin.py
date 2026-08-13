"""
Create the first platform administrator, if there is not one already.

Run automatically on every container start so that a clean checkout reaches a
usable system with one command. It is idempotent: if the account already
exists, nothing changes and no password is reset, so restarting the stack never
silently reverts a password the operator has since changed.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.core.tenancy import bypass_rls


class Command(BaseCommand):
    help = "Create the platform administrator account if it does not exist."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--username", default=settings.PLATFORM_ADMIN_USERNAME)
        parser.add_argument("--password", default=settings.PLATFORM_ADMIN_PASSWORD)
        parser.add_argument("--email", default=settings.PLATFORM_ADMIN_EMAIL)

    def handle(self, *args, **options) -> None:
        username = options["username"]
        password = options["password"]

        if not password:
            self.stdout.write(
                self.style.WARNING(
                    "PLATFORM_ADMIN_PASSWORD is not set; skipping. Set it in "
                    ".env and restart, or create the account with "
                    "`manage.py createsuperuser`."
                )
            )
            return

        # The user table is tenant-isolated, and this account belongs to no
        # tenant, so it can only be written with isolation explicitly lifted.
        with transaction.atomic(), bypass_rls():
            if User.all_objects.filter(username=username, tenant__isnull=True).exists():
                self.stdout.write(f"Platform administrator '{username}' already exists.")
                return

            User.objects.create_superuser(
                username=username,
                password=password,
                full_name="Platform administrator",
                email=options["email"],
            )

        self.stdout.write(
            self.style.SUCCESS(f"Created platform administrator '{username}'.")
        )
